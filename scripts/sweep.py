#!/usr/bin/env python3
"""Per-page SEO sweep over a sitemap (live URL) or a built dist/ folder. Read-only.

    sweep.py https://example.com            # every <loc> in /sitemap.xml
    sweep.py https://example.com/sitemap-0.xml
    sweep.py ./dist                          # prerendered/static HTML files on disk
    --all            print every page, not just problems
    --quick          skip the extra browser/Bingbot fetches (placement check
                     then covers Googlebot only)
    --save FILE      write a JSON snapshot of every page's SEO state
    --compare FILE   diff against an earlier snapshot; exit 1 on critical drift

Exit 2 means incomplete measurement or invalid input. Exit 0 means the scan
completed without critical comparison drift, NOT that the site has no findings.

Per page: HTTP status (redirects are not followed: a sitemap URL that
redirects is a finding, and 302/307 is flagged as temporary), <title> length
(full, incl. brand suffix), meta description 110-160, duplicate titles and
descriptions, canonical present and self-referencing, noindex, one <h1>,
JSON-LD @types (invalid JSON, http:// schema.org context, structured data on a
noindex page), share previews (og:title/description/image, twitter:image,
og:image absolute), HTML size (Googlebot reads the first 2 MB), client-side
redirects (meta refresh, Next.js NEXT_REDIRECT), and headers vs HTML
(X-Robots-Tag, Link rel=canonical).

Placement: title, canonical and description should each appear once INSIDE
<head>, for Googlebot, a browser, and Bingbot. Robots tags are optional and
multiple directives may combine; body placement is flagged separately. Frameworks
that stream metadata (Next.js 15.2+ on dynamic routes) put it in <body> for
some user agents, and Google only honors a canonical in <head>.

Site level (live mode): each distinct share image is fetched once (status,
content type, size, dimensions; want 1200x630 under ~600 KB), non-self
canonical targets must be 200 and indexable, sitemap URLs must not be
blocked by robots.txt for Googlebot, and lastmod dates must not be in the
future or all identical. Sitemap indexes are followed one level.

A present, loading share image can still be broken (overlapping text,
missing font): open every distinct image the summary lists and look at it.
Numbers are what the served HTML contains before JavaScript runs. A fetch
that fails is reported as not measured, never as a finding about the page.
"""

import argparse
import datetime as dt
import html
import json
import re
import struct
import sys
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import urlsplit
from collections import Counter
from pathlib import Path

UAS = {
    "googlebot": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "browser": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "bingbot": "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)",
}
UA = UAS["googlebot"]
DESC_MIN, DESC_MAX, TITLE_MAX = 110, 160, 65
HTML_WARN, HTML_MAX = 1024 * 1024, 2 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def fetch(url, ua=UA):
    """(status, headers, body) without following redirects. status is an int,
    or 'ERR <name>' when the request itself failed (not measured)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": ua})
        with _opener.open(req, timeout=25) as r:
            return r.status, response_headers(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, response_headers(e.headers), e.read() if e.fp else b""
    except Exception as e:  # noqa: BLE001
        return f"ERR {e.__class__.__name__}", {}, b""


def response_headers(headers):
    # Repeated X-Robots-Tag/Link headers all contribute to the response.
    return {k.lower(): ("\n" if k.lower() == "x-robots-tag" else ", ").join(headers.get_all(k))
            for k in headers.keys()} if headers else {}


def text(body):
    return body.decode("utf-8", "replace")


def lower_keys(headers):
    return {k.lower(): v for k, v in (headers or {}).items()}


def image_size(b):
    """(width, height) for PNG/JPEG/GIF/WebP bytes, else None."""
    if len(b) >= 24 and b[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", b[16:24])
    if len(b) >= 10 and b[:6] in (b"GIF87a", b"GIF89a"):
        return struct.unpack("<HH", b[6:10])
    if len(b) >= 30 and b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        if b[12:16] == b"VP8X":
            return 1 + int.from_bytes(b[24:27], "little"), 1 + int.from_bytes(b[27:30], "little")
        if b[12:16] == b"VP8 ":
            w, h = struct.unpack("<HH", b[26:30])
            return w & 0x3FFF, h & 0x3FFF
        return None
    if b[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(b):
            if b[i] != 0xFF:
                i += 1
                continue
            if b[i + 1] in (0xC0, 0xC1, 0xC2):
                h, w = struct.unpack(">HH", b[i + 5:i + 9])
                return w, h
            i += 2 + struct.unpack(">H", b[i + 2:i + 4])[0]
    return None


def sitemap_entries(url):
    """Read a URL set or one level of child sitemaps. Never accept partial coverage."""
    def read(address, allow_index):
        if urlsplit(address).scheme not in ("http", "https"):
            raise ValueError(f"invalid sitemap URL: {address}")
        status, _, body = fetch(address)
        if status != 200:
            raise ValueError(f"sitemap {address} not measured ({status})")
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise ValueError(f"sitemap {address}: invalid XML") from exc
        tag = root.tag.rsplit("}", 1)[-1]
        if tag not in ("urlset", "sitemapindex") or (tag == "sitemapindex" and not allow_index):
            raise ValueError(f"sitemap {address}: expected urlset or one-level sitemap index")
        records = []
        record_tag = "sitemap" if tag == "sitemapindex" else "url"
        for child in root:
            if child.tag.rsplit("}", 1)[-1] != record_tag:
                continue
            fields = {c.tag.rsplit("}", 1)[-1]: (c.text or "").strip() for c in child}
            loc = fields.get("loc", "")
            if urlsplit(loc).scheme not in ("http", "https") or not urlsplit(loc).netloc:
                raise ValueError(f"sitemap {address}: missing or invalid loc {loc!r}")
            if tag == "sitemapindex":
                records.extend(read(loc, False))
            else:
                records.append((loc, fields.get("lastmod") or None))
        if not records:
            raise ValueError(f"sitemap {address}: no URLs; coverage not measured")
        return records
    return list(dict.fromkeys(read(url, True)))


class Document(HTMLParser):
    """Attribute-order-independent tag data with explicit head placement."""
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.in_head = False
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        if tag == "head":
            self.in_head = True
        if tag == "body":
            self.in_head = False
        self.tags.append((tag, dict(attrs), self.in_head))

    def handle_endtag(self, tag):
        if tag == "head":
            self.in_head = False


def document(h):
    return h if isinstance(h, Document) else Document(h)


def meta_values(h, attr, key):
    return [attrs.get("content") or "" for tag, attrs, _ in document(h).tags
            if tag == "meta" and (attrs.get(attr) or "").lower() == key.lower()]


def meta(h, attr, key):
    return next(iter(meta_values(h, attr, key)), None)


def canonical(h):
    return next((attrs.get("href") or "" for tag, attrs, _ in document(h).tags
                 if tag == "link" and "canonical" in (attrs.get("rel") or "").lower().split()), "")


def has_noindex(value):
    return bool(set(re.split(r"[\s,]+", value.lower())) & {"noindex", "none"})


def header_noindex(value, who="googlebot"):
    # A crawler prefix scopes following directives until another prefix appears.
    if "\n" in value:
        return any(header_noindex(line, who) for line in value.splitlines())
    scope = None
    for part in value.lower().split(","):
        part = part.strip()
        match = re.match(r"([a-z][\w-]*):\s*(.*)", part)
        if match:
            prefix, directive = match.groups()
            if prefix not in ("unavailable_after", "max-snippet", "max-image-preview", "max-video-preview"):
                scope, part = prefix, directive
        if scope in (None, who) and has_noindex(part):
            return True
    return False


def effective_noindex(h, headers=None, who="googlebot"):
    values = meta_values(h, "name", "robots") + meta_values(h, "name", who)
    return any(has_noindex(v) for v in values) or header_noindex(lower_keys(headers).get("x-robots-tag", ""), who)


def placement_problems(h, who):
    """Report missing, duplicated, or body metadata; optional robots tags may combine."""
    groups = {key: [] for key in ("title", "canonical", "description", "robots")}
    for tag, attrs, in_head in document(h).tags:
        key = None
        if tag == "title":
            key = "title"
        elif tag == "link" and "canonical" in (attrs.get("rel") or "").lower().split():
            key = "canonical"
        elif tag == "meta" and (attrs.get("name") or "").lower() in ("description", "robots"):
            key = attrs["name"].lower()
        if key:
            groups[key].append(in_head)
    out = []
    for key, locations in groups.items():
        if False in locations:
            out.append(f"{key} in <body> for {who}")
        if key != "robots" and sum(locations) != 1:
            out.append(f"{key} x{sum(locations)} in <head> for {who}")
    return out


def jsonld_types(data):
    if isinstance(data, list):
        return [kind for node in data for kind in jsonld_types(node)]
    if not isinstance(data, dict):
        return ["INVALID"]
    out = []
    if "@type" in data:
        types = data["@type"] if isinstance(data["@type"], list) else [data["@type"]]
        out.extend(kind if isinstance(kind, str) else "INVALID" for kind in types)
    if "@graph" in data:
        out.extend(jsonld_types(data["@graph"]))
    return out


def blank_row(url, status, problem):
    return dict(label=url, status=status, title="", desc="", canonical="", robots="", h1=0, types=[],
                og_image="", noindex=False, x_robots_tag="", ua_checks={}, problems=[problem])


def analyze(label, status, h, headers=None, self_url=None, size=None):
    headers = lower_keys(headers)
    doc = document(h)
    t = re.search(r"<title[^>]*>(.*?)</title>", h, re.S | re.I)
    title = html.unescape(re.sub(r"\s+", " ", t.group(1)).strip()) if t else ""
    desc = meta(doc, "name", "description") or ""
    canon = canonical(doc)
    robots = ", ".join(meta_values(doc, "name", "robots")).lower()
    noindex = effective_noindex(doc, headers)
    h1s = len(re.findall(r"<h1[\s>]", h, re.I))
    types, http_ctx = [], False
    for block in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', h, re.S | re.I):
        http_ctx = http_ctx or '"http://schema.org' in block
        try:
            data = json.loads(block)
            types.extend(jsonld_types(data))
        except json.JSONDecodeError:
            types.append("INVALID")
    og_title = meta(doc, "property", "og:title")
    og_desc = meta(doc, "property", "og:description")
    og_image = meta(doc, "property", "og:image") or ""
    tw_image = meta(doc, "name", "twitter:image") or ""

    problems = []
    if status != 200:
        problems.append(f"status {status}")
    if not title:
        problems.append("no <title>")
    elif len(title) > TITLE_MAX:
        problems.append(f"title {len(title)}>{TITLE_MAX}")
    if not desc:
        problems.append("no description")
    elif not DESC_MIN <= len(desc) <= DESC_MAX:
        problems.append(f"description {len(desc)} (want {DESC_MIN}-{DESC_MAX})")
    if noindex:
        problems.append("noindex")
    if not canon:
        problems.append("no canonical")
    elif self_url and canon.rstrip("/") != self_url.rstrip("/"):
        problems.append(f"canonical -> {canon}")
    if h1s != 1:
        problems.append(f"{h1s} <h1>")
    if "INVALID" in types:
        problems.append("invalid JSON-LD")
    if http_ctx:
        problems.append("JSON-LD uses http://schema.org (use https)")
    if types and noindex:
        problems.append("structured data on a noindex page")
    if not og_title:
        problems.append("no og:title")
    if not og_desc:
        problems.append("no og:description")
    if not og_image:
        problems.append("no og:image")
    elif not og_image.startswith(("http://", "https://")):
        problems.append("og:image not absolute")
    if not tw_image and not og_image:
        problems.append("no twitter:image")
    if re.search(r'<meta[^>]*http-equiv=["\']refresh["\']', h, re.I) or "NEXT_REDIRECT" in h:
        problems.append("client-side redirect (use a server 301/308)")
    if size:
        if size > HTML_MAX:
            problems.append(f"html {size // 1024} KB (>2 MB: Googlebot truncates)")
        elif size > HTML_WARN:
            problems.append(f"html {size // 1024} KB (>1 MB)")
    xrt = headers.get("x-robots-tag", "").lower()
    if xrt and header_noindex(xrt) != has_noindex(robots):
        problems.append(f"X-Robots-Tag '{xrt}' disagrees with meta robots '{robots or '-'}'")
    hc = re.search(r'<([^>]+)>\s*;\s*rel="?canonical"?', headers.get("link", ""), re.I)
    if hc and canon and hc.group(1).rstrip("/") != canon.rstrip("/"):
        problems.append(f"Link header canonical {hc.group(1)} != HTML {canon}")
    problems += placement_problems(doc, "googlebot")
    return dict(label=label, status=status, title=title, desc=desc, canonical=canon, robots=robots,
                h1=h1s, types=types, og_image=og_image, noindex=noindex, x_robots_tag=xrt, ua_checks={}, problems=problems)


def snapshot(rows):
    keys = ("status", "title", "desc", "canonical", "robots", "h1", "types", "og_image", "noindex", "x_robots_tag", "ua_checks", "problems")
    return {r["label"]: {k: r[k] for k in keys} for r in rows}


def compare(old, new):
    """(critical, warnings), including legacy snapshots without header/UA fields."""
    crit, warn = [], []
    for url, o in old.items():
        n = new.get(url)
        if n is None:
            warn.append(f"{url}: gone from sitemap")
            continue
        if not isinstance(n["status"], int) or not isinstance(o["status"], int):
            warn.append(f"{url}: not measured in one snapshot; content drift not assessed")
            continue
        if n["status"] != 200:
            if o["status"] == 200:
                crit.append(f"{url}: status {o['status']} -> {n['status']}")
            continue
        if o["status"] != 200:
            warn.append(f"{url}: now HTTP 200; no successful content baseline")
            continue
        old_noindex = o.get("noindex", has_noindex(o.get("robots", "")))
        new_noindex = n.get("noindex", has_noindex(n.get("robots", "")))
        if new_noindex and not old_noindex:
            crit.append(f"{url}: noindex added")
        if o["canonical"] and n["canonical"] != o["canonical"]:
            crit.append(f"{url}: canonical {o['canonical']} -> {n['canonical'] or 'missing'}")
        # Flatten type arrays from older snapshots as well as normalized v2 rows.
        def type_set(value):
            return {t for x in value for t in (x if isinstance(x, list) else [x]) if isinstance(t, str)}
        lost = sorted(type_set(o["types"]) - type_set(n["types"]))
        if lost:
            crit.append(f"{url}: JSON-LD removed: {', '.join(lost)}")
        if o["og_image"] and not n["og_image"]:
            crit.append(f"{url}: og:image removed")
        for k in ("title", "desc"):
            if n[k] != o[k]:
                warn.append(f"{url}: {k} changed")
        if n["h1"] != o["h1"]:
            warn.append(f"{url}: h1 count {o['h1']} -> {n['h1']}")
        for who, previous in o.get("ua_checks", {}).items():
            current = n.get("ua_checks", {}).get(who)
            if current is None or not isinstance(current["status"], int):
                warn.append(f"{url}: {who} not measured; UA drift not assessed")
            elif previous["status"] == 200:
                if current["status"] != 200:
                    crit.append(f"{url}: {who} status 200 -> {current['status']}")
                elif current.get("noindex") and not previous.get("noindex"):
                    crit.append(f"{url}: {who} noindex added")
    for url in sorted(new.keys() - old.keys()):
        warn.append(f"{url}: new in sitemap")
    return crit, warn


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target")
    parser.add_argument("--save")
    parser.add_argument("--compare")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--quick", action="store_true")
    opts = parser.parse_args()
    target, show_all, quick = opts.target, opts.all, opts.quick
    live = not Path(target).is_dir()
    rows, site_notes, incomplete = [], [], []
    entries = []

    if not live:
        for f in sorted(Path(target).rglob("*.html")):
            b = f.read_bytes()
            rows.append(analyze(str(f.relative_to(target)), 200, text(b), size=len(b)))
    else:
        sm = target if target.endswith(".xml") else target.rstrip("/") + "/sitemap.xml"
        try:
            entries = sitemap_entries(sm)
        except ValueError as exc:
            # Fail closed: never overwrite a previous baseline with partial sitemap data.
            print(f"INCOMPLETE: {exc}", file=sys.stderr)
            sys.exit(2)
        print(f"{len(entries)} URLs from {sm}", file=sys.stderr)
        for u, _ in entries:
            status, headers, body = fetch(u)
            if isinstance(status, int) and 300 <= status < 400:
                loc = lower_keys(headers).get("location", "?")
                kind = "temporary" if status in (302, 303, 307) else "permanent"
                rows.append(blank_row(u, status, f"sitemap URL redirects ({status} {kind} -> {loc}); list the final URL"))
                continue
            if not isinstance(status, int):
                incomplete.append(f"{u}: googlebot not measured ({status})")
                rows.append(blank_row(u, status, f"not measured ({status})"))
                continue
            if status != 200:
                rows.append(blank_row(u, status, f"status {status}; content not assessed"))
                if status in (401, 403, 429) or status >= 500:
                    incomplete.append(f"{u}: content not measured (HTTP {status}; possible access/rate/server failure)")
                continue
            r = analyze(u, status, text(body), headers, self_url=u, size=len(body))
            r["ua_checks"]["googlebot"] = {"status": status, "noindex": r["noindex"]}
            if not quick:
                for who in ("browser", "bingbot"):
                    s2, h2, b2 = fetch(u, UAS[who])
                    r["ua_checks"][who] = {"status": s2, "noindex": effective_noindex(text(b2), h2, who) if s2 == 200 else None}
                    if s2 == 200:
                        r["problems"] += placement_problems(text(b2), who)
                        if r["ua_checks"][who]["noindex"] and not r["noindex"]:
                            r["problems"].append(f"noindex for {who}")
                    else:
                        incomplete.append(f"{u}: {who} placement not measured ({s2})")
                        r["problems"].append(f"{who} placement not measured ({s2})")
            rows.append(r)

        # Reuse fetched targets, but validate their status and effective robots state.
        fetched = {r["label"].rstrip("/"): r for r in rows}
        for canon in sorted({r["canonical"] for r in rows if r["canonical"]}):
            sources = [r for r in rows if r["canonical"] == canon and r["label"].rstrip("/") != canon.rstrip("/")]
            if not sources:
                continue
            target_row = fetched.get(canon.rstrip("/"))
            if target_row:
                status, noindex = target_row["status"], target_row["noindex"]
            else:
                status, headers, body = fetch(canon)
                noindex = status == 200 and effective_noindex(text(body), headers)
            if not isinstance(status, int):
                incomplete.append(f"canonical target {canon} not measured ({status})")
            elif status != 200 or noindex:
                for r in sources:
                    r["problems"].append(f"canonical target {canon} is {'noindex' if noindex else status}")

        # robots.txt vs sitemap.
        base = re.match(r"https?://[^/]+", entries[0][0]).group(0) if entries else target.rstrip("/")
        rs, _, rb = fetch(base + "/robots.txt")
        if rs == 200:
            rp = urllib.robotparser.RobotFileParser()
            rp.parse(text(rb).splitlines())
            for r in rows:
                if not rp.can_fetch("Googlebot", r["label"]):
                    r["problems"].append("in sitemap but blocked by robots.txt")
        else:
            incomplete.append(f"robots.txt not measured ({rs})")

        # lastmod sanity.
        lms = [lm for _, lm in entries if lm]
        future = [lm for lm in lms if lm[:10] > dt.date.today().isoformat()]
        if future:
            site_notes.append(f"{len(future)} sitemap lastmod dates are in the future")
        if len(lms) > 5 and len(set(lms)) == 1:
            site_notes.append("every sitemap lastmod is identical (looks generated, not real)")

    tdup = {k for k, c in Counter(r["title"] for r in rows if r["title"]).items() if c > 1}
    ddup = {k for k, c in Counter(r["desc"] for r in rows if r["desc"]).items() if c > 1}
    for r in rows:
        if r["title"] in tdup:
            r["problems"].append("duplicate title")
        if r["desc"] in ddup:
            r["problems"].append("duplicate description")

    # Share images: fetch each distinct one once (live mode only).
    image_notes = []
    if live:
        images = Counter(r["og_image"] for r in rows if r["og_image"].startswith(("http://", "https://")))
        for url, n in images.most_common():
            status, headers, body = fetch(url)
            ctype = lower_keys(headers).get("content-type", "")
            if not isinstance(status, int):
                incomplete.append(f"share image {url} not measured ({status})")
                continue
            dims = image_size(body) if body else None
            kb = len(body) // 1024
            flags = []
            if status != 200:
                flags.append(f"status {status}")
            elif not ctype.startswith("image/"):
                flags.append(f"content-type {ctype or '?'}")
            if dims and dims != (1200, 630):
                flags.append(f"{dims[0]}x{dims[1]} (want 1200x630)")
            if not dims and status == 200:
                flags.append("unreadable image")
            if kb > 600:
                flags.append(f"{kb} KB (>600 KB; WhatsApp may drop the preview)")
            image_notes.append((url, n, kb, dims, flags))
            if flags:
                for r in rows:
                    if r["og_image"] == url:
                        r["problems"].append("og:image " + "; ".join(flags))

    for r in rows:
        if show_all or r["problems"]:
            print(f"{r['label']}\n  title({len(r['title'])}): {r['title']}\n  desc({len(r['desc'])}): {r['desc']}")
            print(f"  jsonld: {', '.join(r['types']) or '-'}   og:image: {r['og_image'] or '-'}")
            print(f"  problems: {'; '.join(r['problems']) or 'none'}")

    if not rows:
        incomplete.append("no HTML pages measured")
    measured = [r for r in rows if r["status"] == 200]
    dl = [len(r["desc"]) for r in measured if r["desc"]]
    buckets = Counter(
        "missing" if not r["desc"] else "<110" if len(r["desc"]) < DESC_MIN else ">160" if len(r["desc"]) > DESC_MAX else "ok"
        for r in measured
    )

    def kind(p):
        for prefix in ("duplicate", "no ", "og:image", "canonical target", "client-side", "sitemap URL redirects",
                       "in sitemap but", "structured data", "JSON-LD uses", "X-Robots-Tag", "Link header", "not measured"):
            if p.startswith(prefix):
                return prefix.strip()
        if " for googlebot" in p or " for browser" in p or " for bingbot" in p:
            return "placement: " + p.rsplit(" for ", 1)[-1]
        if p.endswith("<h1>"):
            return "h1 count != 1"
        return p.split(" ")[0]  # status / title / description / canonical / html / noindex

    kinds = Counter(kind(p) for r in rows for p in r["problems"])
    print("\n== Summary")
    print(f"pages: {len(rows)}  with problems: {sum(1 for r in rows if r['problems'])}")
    print(f"descriptions (HTTP 200 pages only): {dict(buckets)}  range: {min(dl) if dl else '-'}-{max(dl) if dl else '-'}")
    print(f"problem kinds: {dict(kinds)}")
    for note in site_notes:
        print(f"site: {note}")
    if image_notes:
        print("share images (open each and look at it; a loading image can still be broken):")
        for url, n, kb, dims, flags in image_notes:
            d = f"{dims[0]}x{dims[1]}" if dims else "?"
            print(f"  {n:>4} pages  {d}  {kb} KB  {url}  {'; '.join(flags) or 'ok'}")

    for note in incomplete:
        print(f"INCOMPLETE: {note}")
    snap = snapshot(rows)
    if opts.save:
        dest = Path(opts.save)
        if opts.compare and dest.resolve() == Path(opts.compare).resolve():
            parser.error("--save and --compare must be different files")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps({"version": 2, "taken": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                                   "complete": not incomplete, "incomplete": incomplete,
                                   "requested_user_agents": list(UAS) if live and not quick else ["googlebot"] if live else [],
                                   "pages": snap}, indent=1) + "\n")
        print(f"snapshot saved: {dest} ({len(snap)} pages)")
    crit = []
    if opts.compare:
        old = json.loads(Path(opts.compare).read_text())
        crit, warn = compare(old["pages"], snap)
        print(f"\n== Drift vs {opts.compare} (taken {old.get('taken', '?')})")
        if "version" not in old:
            warn.insert(0, "legacy baseline: prior HTTP robots headers and UA coverage are unknown")
        if old.get("complete") is False:
            warn.insert(0, "baseline was incomplete; unmeasured prior state cannot be compared")
        for c in crit:
            print(f"  CRITICAL {c}")
        for w in warn:
            print(f"  warning  {w}")
        if not crit and not warn and not incomplete:
            print("  no changes")
    # A transport/coverage failure is a verification failure, never fabricated drift.
    if incomplete:
        sys.exit(2)
    if crit:
        sys.exit(1)


if __name__ == "__main__":
    main()
