#!/usr/bin/env bash
# Host and crawl hygiene check, fetched as Googlebot. Read-only.
#
#   hosts.sh example.com [alt-domain.com ...]
#
# Reports, for the canonical host and every alternate domain you pass:
#   - status + Location for https/http/www variants (want one 301/308 hop to
#     the canonical https host)
#   - whether an alternate domain serves the full site (200) instead of
#     redirecting — a duplicate-site problem even when canonicals are right
#   - robots.txt, sitemap.xml, and X-Robots-Tag headers
#   - a junk URL, to catch soft 404s (200 + app shell for pages that don't exist)
#   - staging/dev/preview/beta subdomains (want no DNS, a redirect, auth, or
#     noindex; custom preview domains are NOT auto-noindexed on Vercel). Pass
#     platform aliases (project.vercel.app, site.netlify.app) as alt hosts.
#   - crawler access per robots.txt token: search bots, AI search bots, and AI
#     training bots are separate tokens; report which are allowed on / so the
#     policy is a decision, not an accident. robots.txt 5xx = "disallow all".
set -u
PROBE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/seo-tune-hosts.XXXXXX")
trap 'rm -rf "$PROBE_DIR"' EXIT
MAIN="${1:?usage: hosts.sh canonical-host [alt-hosts...]}"
shift
UA="Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"

head_line() {
  curl -sS -o /dev/null -A "$UA" --max-time 15 \
    -w "%{http_code} %{redirect_url}" "$1" 2>&1
  rc=$?
  if [ "$rc" -ne 0 ]; then printf " (not measured: curl exit %s)" "$rc"; fi
}

echo "== Canonical host variants"
for u in "https://$MAIN/" "http://$MAIN/" "https://www.$MAIN/" "http://www.$MAIN/"; do
  printf '%-40s %s\n' "$u" "$(head_line "$u")"
done

for alt in "$@"; do
  echo
  echo "== Alternate domain: $alt (want 301/308 to https://$MAIN for marketing paths)"
  for p in / /pricing /features /blog; do
    printf '%-40s %s\n' "https://$alt$p" "$(head_line "https://$alt$p")"
  done
done

echo
echo "== Crawl files and headers"
for p in /robots.txt /sitemap.xml /; do
  code=$(curl -sS -o /dev/null -A "$UA" --max-time 15 -w "%{http_code}" "https://$MAIN$p")
  xrt=$(curl -sS -D - -o /dev/null -A "$UA" --max-time 15 "https://$MAIN$p" | grep -i '^x-robots-tag' | tr -d '\r')
  printf '%-40s %s %s\n' "https://$MAIN$p" "$code" "${xrt:-}"
done

echo
echo "== Soft 404 probe (want 404/410, or 200 with noindex)"
junk="https://$MAIN/seo-tune-probe-$(date +%s)"
body=$(curl -sS -A "$UA" --max-time 15 -w '\n__STATUS__%{http_code}' "$junk")
status=${body##*__STATUS__}
title=$(printf '%s' "$body" | grep -o '<title[^>]*>[^<]*' | head -1 | sed 's/<title[^>]*>//')
robots=$(printf '%s' "$body" | grep -oi '<meta[^>]*name="robots"[^>]*>' | head -1)
echo "$junk"
echo "  status: $status  title: ${title:-<none>}  robots: ${robots:-<none>}"

echo
echo "== Staging / preview subdomains (want no DNS, redirect, 401/403, or noindex)"
for sub in staging dev preview beta test; do
  u="https://$sub.$MAIN/"
  : > "$PROBE_DIR/body"
  out=$(curl -sS -D - -o "$PROBE_DIR/body" -A "$UA" --max-time 10 -w '__STATUS__%{http_code} %{redirect_url}' "$u" 2>&1)
  rc=$?
  status=${out##*__STATUS__}
  xrt=$(printf '%s' "$out" | grep -i '^x-robots-tag' | tr -d '\r' | head -1)
  meta=$(grep -oi '<meta[^>]*name="robots"[^>]*>' "$PROBE_DIR/body" 2>/dev/null | head -1)
  if [ "$rc" -ne 0 ]; then
    if [ "$rc" -eq 6 ]; then verdict="DNS resolution failed (not measured)"
    else verdict="not measured (curl exit $rc: transport, TLS, timeout, or sandbox failure)"; fi
  else
  case "$status" in
    000*) verdict="not measured (no HTTP response)";;
    30*|401*|403*|404*) verdict="ok";;
    200*) if printf '%s %s' "$xrt" "$meta" | grep -qi noindex; then verdict="200 but noindex (ok)"; else verdict="200 and indexable: PROBLEM"; fi;;
    *) verdict="check by hand";;
  esac
  fi
  printf '%-40s %s  %s\n' "$u" "${status%% *}" "$verdict"
done


echo
echo "== Crawler access to / per robots.txt (decide this on purpose)"
python3 - "https://$MAIN" <<'PY'
import sys, urllib.request, urllib.robotparser
base = sys.argv[1]
try:
    with urllib.request.urlopen(urllib.request.Request(base + "/robots.txt", headers={"User-Agent": "Mozilla/5.0"}), timeout=15) as r:
        status, text = r.status, r.read().decode("utf-8", "replace")
except urllib.error.HTTPError as e:
    status, text = e.code, ""
except Exception as e:  # noqa: BLE001
    print(f"  robots.txt not measured ({e.__class__.__name__})"); sys.exit()
if status >= 500:
    print(f"  robots.txt HTTP {status}: crawlers treat this as disallow-all. PROBLEM"); sys.exit()
rp = urllib.robotparser.RobotFileParser(); rp.parse(text.splitlines())
groups = {
    "search": ["Googlebot", "Bingbot", "Applebot", "DuckDuckBot"],
    "AI search / user fetch": ["OAI-SearchBot", "ChatGPT-User", "Claude-SearchBot", "Claude-User", "PerplexityBot"],
    "AI training": ["GPTBot", "ClaudeBot", "Google-Extended", "Applebot-Extended", "CCBot", "Meta-ExternalAgent"],
}
for label, bots in groups.items():
    print(f"  {label:24} " + "  ".join(f"{b}:{'allow' if rp.can_fetch(b, base + '/') else 'BLOCK'}" for b in bots))
print(f"  (robots.txt HTTP {status}; Google-Extended only affects Gemini training, never Search)")
PY

echo
echo "== robots.txt (Googlebot/Bingbot groups and Disallow lines)"
curl -sS -A "$UA" --max-time 15 "https://$MAIN/robots.txt" | grep -iE '^(user-agent|disallow|allow|sitemap)' | head -40
