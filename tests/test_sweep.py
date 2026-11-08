"""Offline regression tests. Run: python3 -m unittest discover -s tests -v."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sweep", ROOT / "scripts/sweep.py")
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)
URL = "https://example.test/"


def page(extra="", canonical=None, body=""):
    return ('<html><head><title>Example page</title>'
            '<meta name="description" content="' + 'd' * 130 + '">'
            + (canonical or f'<link rel="canonical" href="{URL}">')
            + '<meta property="og:title" content="Example page">'
            '<meta property="og:description" content="Example description">'
            '<meta property="og:image" content="https://example.test/card.png">'
            '<meta name="twitter:image" content="https://example.test/card.png">'
            '<meta name="twitter:card" content="summary_large_image">'
            + extra + '</head><body><h1>Example</h1><p>' + 'word ' * 40 + '</p>' + body + '</body></html>')


def row(html=None, headers=None):
    return s.analyze(URL, 200, html or page(), headers)


def cli(args):
    output = io.StringIO()
    with patch.object(sys, "argv", ["sweep.py", *args]), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        try:
            s.main()
            code = 0
        except SystemExit as exc:
            code = exc.code
    return code, output.getvalue()


class SweepTests(unittest.TestCase):
    def test_clean_page(self):
        self.assertEqual(row()["problems"], [])

    def test_header_noindex_drift(self):
        before, after = row(), row(headers={"X-Robots-Tag": "noindex"})
        critical, _ = s.compare(s.snapshot([before]), s.snapshot([after]))
        self.assertTrue(any("noindex" in x for x in critical))

    def test_meta_noindex_drift_positive_control(self):
        critical, _ = s.compare(s.snapshot([row()]), s.snapshot([row(page('<meta name="robots" content="noindex">'))]))
        self.assertTrue(any("noindex" in x for x in critical))

    def test_robot_scope_and_none(self):
        for headers, expected in [({"X-Robots-Tag": "bingbot: noindex"}, False),
                                  ({"X-Robots-Tag": "googlebot: none"}, True),
                                  ({"X-Robots-Tag": "noindex, bingbot: index"}, True)]:
            with self.subTest(headers=headers):
                self.assertEqual(row(headers=headers)["noindex"], expected)
        self.assertTrue(row(page('<meta name="googlebot" content="none">'))["noindex"])

    def test_repeated_robot_headers_keep_independent_scopes(self):
        from email.message import Message
        headers = Message()
        headers.add_header('X-Robots-Tag', 'bingbot: noindex')
        headers.add_header('X-Robots-Tag', 'noindex')
        self.assertTrue(row(headers=s.response_headers(headers))['noindex'])

    def test_failed_fetch_not_reported_as_removal(self):
        failed = s.blank_row(URL, "ERR URLError", "not measured")
        critical, warnings = s.compare(s.snapshot([row()]), s.snapshot([failed]))
        self.assertEqual(critical, [])
        self.assertTrue(any("not measured" in x for x in warnings))
        self.assertFalse(any("removed" in x or "changed" in x for x in warnings))

    def test_http_failure_reports_status_only(self):
        failed = s.blank_row(URL, 404, "status 404")
        critical, _ = s.compare(s.snapshot([row()]), s.snapshot([failed]))
        self.assertEqual(len(critical), 1)
        self.assertIn("status", critical[0])

    def test_legacy_snapshot_compatible(self):
        before = s.snapshot([row()])
        for value in before.values():
            for key in ("noindex", "x_robots_tag", "ua_checks", "problems"):
                value.pop(key, None)
        critical, _ = s.compare(before, s.snapshot([row()]))
        self.assertEqual(critical, [])

    def test_other_critical_drift_still_fails(self):
        for field, value in [('canonical', URL + 'moved'), ('types', []), ('og_image', ''), ('status', 404)]:
            with self.subTest(field=field):
                before = row(); before['types'] = ['Organization']
                after = {**before, field: value}
                critical, _ = s.compare(s.snapshot([before]), s.snapshot([after]))
                self.assertTrue(critical)

    def test_title_change_is_warning(self):
        before = row(); after = {**before, 'title': 'Updated title'}
        critical, warnings = s.compare(s.snapshot([before]), s.snapshot([after]))
        self.assertEqual(critical, [])
        self.assertTrue(any('title changed' in warning for warning in warnings))

    def test_canonical_attribute_order_entities_and_whitespace(self):
        h = page(canonical='<LINK href="https://example.test/?a=1&amp;b=2" rel = "canonical">')
        r = row(h)
        self.assertEqual(r["canonical"], "https://example.test/?a=1&b=2")
        self.assertNotIn("no canonical", r["problems"])
        self.assertEqual(s.placement_problems(h, "browser"), [])

    def test_body_metadata_detected(self):
        h = page().replace(f'<link rel="canonical" href="{URL}">', '').replace('</body>', f'<link href="{URL}" rel="canonical"></body>')
        self.assertIn("canonical in <body> for browser", s.placement_problems(h, "browser"))

    def test_array_and_graph_jsonld(self):
        for data in [{"@type": ["Organization", "LocalBusiness"]},
                     {"@graph": [{"@type": "Organization"}, {"@type": "LocalBusiness"}]}]:
            with self.subTest(data=data):
                r = row(page('<script type="application/ld+json">' + json.dumps(data) + '</script>'))
                self.assertEqual(set(r["types"]), {"Organization", "LocalBusiness"})
                self.assertEqual(s.compare(s.snapshot([r]), s.snapshot([r])), ([], []))

    def test_malformed_jsonld_does_not_crash(self):
        for data in ('null', '42', '"string"', '{broken', '{"@type": {"bad": true}}'):
            with self.subTest(data=data):
                self.assertIn("invalid JSON-LD", row(page('<script type="application/ld+json">' + data + '</script>'))["problems"])

    def test_sitemap_child_failures_rejected(self):
        index = b'<sitemapindex><sitemap><loc>https://example.test/child.xml</loc></sitemap></sitemapindex>'
        for status, body in [(503, b'error'), ('ERR URLError', b''), (200, b'<html>not XML sitemap</html>')]:
            with self.subTest(status=status), patch.object(s, "fetch", side_effect=[(200, {}, index), (status, {}, body)]):
                with self.assertRaises((ValueError, SystemExit)):
                    s.sitemap_entries(URL + 'sitemap.xml')

    def test_sitemap_namespaces_and_entities(self):
        body = b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://example.test/?a=1&amp;b=2</loc></url></urlset>'
        with patch.object(s, "fetch", return_value=(200, {}, body)):
            self.assertEqual(s.sitemap_entries(URL + 'sitemap.xml'), [(URL + '?a=1&b=2', None)])

    def test_save_creates_parent_and_records_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'index.html').write_text(page())
            dest = root / 'new' / 'snapshot.json'
            code, _ = cli([str(root), '--save', str(dest)])
            self.assertEqual(code, 0)
            data = json.loads(dest.read_text())
            self.assertEqual(data['version'], 2)
            self.assertTrue(data['complete'])

    def test_empty_sitemap_is_incomplete(self):
        with patch.object(s, "fetch", return_value=(200, {}, b'<urlset/>')):
            code, output = cli([URL])
        self.assertEqual(code, 2)
        self.assertIn('INCOMPLETE', output)

    def test_truncated_image_does_not_crash(self):
        self.assertIsNone(s.image_size(b'\x89PNG\r\n\x1a\n'))

    def run_live_mock(self, override, *args):
        from fixture_server import IMAGES
        self.requested = requested = []
        def fetch(url, ua=s.UA, limit=None):
            requested.append(url)
            result = override(url, ua)
            if result is not None:
                return result
            if url.endswith('/sitemap.xml'):
                return 200, {}, f'<urlset><url><loc>{URL}</loc></url></urlset>'.encode()
            if url.endswith('/robots.txt'):
                return 200, {}, b'User-agent: *\nAllow: /'
            if url.endswith('/card.png'):
                return 200, {'Content-Type': 'image/png'}, IMAGES['/img.png']
            return 200, {}, page().encode()
        with tempfile.TemporaryDirectory() as tmp, patch.object(s, 'fetch', side_effect=fetch):
            dest = Path(tmp) / 'snapshot.json'
            code, output = cli([URL, '--save', str(dest), *args])
            return code, output, json.loads(dest.read_text())

    def test_browser_outage_recorded_and_exits_incomplete(self):
        code, output, snap = self.run_live_mock(lambda url, ua: (503, {}, b'bad') if url == URL and ua == s.UAS['browser'] else None)
        self.assertEqual(code, 2)
        self.assertIn('browser placement not measured (503)', output)
        self.assertFalse(snap['complete'])
        self.assertEqual(snap['pages'][URL]['ua_checks']['browser']['status'], 503)

    def test_primary_network_failure_not_missing_metadata(self):
        code, output, snap = self.run_live_mock(lambda url, ua: ('ERR URLError', {}, b'') if url == URL else None)
        self.assertEqual(code, 2)
        self.assertFalse(snap['complete'])
        self.assertNotIn('no canonical', output)
        self.assertNotIn("'missing': 1", output)

    def test_blocked_response_does_not_invent_metadata_findings(self):
        code, output, snap = self.run_live_mock(lambda url, ua: (403, {}, b'Access denied') if url == URL else None)
        self.assertEqual(code, 2)
        self.assertFalse(snap['complete'])
        self.assertNotIn('no canonical', output)

    def test_asset_network_failure_is_incomplete(self):
        code, output, snap = self.run_live_mock(lambda url, ua: ('ERR URLError', {}, b'') if url.endswith('/card.png') else None)
        self.assertEqual(code, 2)
        self.assertIn('share image', output)
        self.assertFalse(snap['complete'])
        self.assertNotIn('og:image status ERR', output)

    def test_canonical_network_failure_is_incomplete(self):
        def override(url, ua):
            if url == URL:
                return 200, {}, page(canonical='<link rel="canonical" href="https://example.test/target">').encode()
            if url.endswith('/target'):
                return 'ERR URLError', {}, b''
        code, output, snap = self.run_live_mock(override)
        self.assertEqual(code, 2)
        self.assertIn('canonical target https://example.test/target not measured', output)
        self.assertFalse(snap['complete'])

    def test_canonical_header_noindex_is_checked(self):
        def override(url, ua):
            if url == URL:
                return 200, {}, page(canonical='<link rel="canonical" href="https://example.test/target">').encode()
            if url.endswith('/target'):
                return 200, {'X-Robots-Tag': 'noindex'}, page().encode()
        code, output, _ = self.run_live_mock(override)
        self.assertEqual(code, 0)  # Audit findings are reported, not a scan failure.
        self.assertIn('canonical target https://example.test/target is noindex', output)

    def test_canonical_target_already_in_sitemap_checked(self):
        def override(url, ua):
            if url.endswith('/sitemap.xml'):
                return 200, {}, f'<urlset><url><loc>{URL}</loc></url><url><loc>{URL}target</loc></url></urlset>'.encode()
            if url == URL:
                return 200, {}, page(canonical=f'<link rel="canonical" href="{URL}target">').encode()
            if url.endswith('/target'):
                return 200, {'X-Robots-Tag': 'noindex'}, page(canonical=f'<link rel="canonical" href="{URL}target">').encode()
        code, output, _ = self.run_live_mock(override)
        self.assertEqual(code, 0)
        self.assertIn(f'canonical target {URL}target is noindex', output)

    def test_comparison_detects_bing_only_noindex(self):
        before = row(); before['ua_checks'] = {'bingbot': {'status': 200, 'noindex': False}}
        after = {**before, 'ua_checks': {'bingbot': {'status': 200, 'noindex': True}}}
        critical, _ = s.compare(s.snapshot([before]), s.snapshot([after]))
        self.assertTrue(any('bingbot noindex added' in change for change in critical))

    def test_metadata_like_text_in_script_is_not_a_tag(self):
        h = page('<script>var s = "<link href=broken rel=canonical>";</script>')
        self.assertEqual(s.placement_problems(h, 'googlebot'), [])

    def test_bing_header_noindex_recorded(self):
        code, _, snap = self.run_live_mock(lambda url, ua: (200, {'X-Robots-Tag': 'bingbot: noindex'}, page().encode()) if url == URL else None)
        self.assertEqual(code, 0)
        self.assertTrue(snap['pages'][URL]['ua_checks']['bingbot']['noindex'])
        self.assertFalse(snap['pages'][URL]['noindex'])

    def test_trailing_slash_canonical_mismatch_is_reported(self):
        r = s.analyze(URL + 'pricing', 200, page(canonical=f'<link rel="canonical" href="{URL}pricing/">'), self_url=URL + 'pricing')
        self.assertTrue(any('canonical ->' in p and 'trailing slash' in p for p in r['problems']), r['problems'])

    def test_root_without_slash_is_same_url(self):
        r = s.analyze(URL, 200, page(canonical='<link rel="canonical" href="https://example.test">'), self_url=URL)
        self.assertFalse(any('canonical ->' in p for p in r['problems']), r['problems'])

    def test_canonical_target_trailing_slash_form_is_fetched(self):
        def override(url, ua):
            if url.endswith('/sitemap.xml'):
                return 200, {}, f'<urlset><url><loc>{URL}a</loc></url></urlset>'.encode()
            if url == URL + 'a':
                return 200, {}, page(canonical=f'<link rel="canonical" href="{URL}a/">').encode()
            if url == URL + 'a/':
                return 308, {'Location': URL + 'a'}, b''
        code, output, _ = self.run_live_mock(override)
        self.assertEqual(code, 0)
        self.assertIn(f'canonical target {URL}a/ is 308', output)

    def test_robots_longest_match_and_wildcards(self):
        rules = s.Robots('User-agent: *\nDisallow: /_next/\nAllow: /_next/static/\nDisallow: /*?q=\nDisallow: /*.pdf$\n')
        for path, expected in [('/_next/static/a.js', True), ('/_next/data/a.json', False), ('/search?q=1', False),
                               ('/search', True), ('/doc.pdf', False), ('/doc.pdf?v=1', True), ('/', True)]:
            with self.subTest(path=path):
                self.assertEqual(rules.allowed('Googlebot', 'https://example.test' + path), expected)

    def test_robots_tie_goes_to_allow(self):
        rules = s.Robots('User-agent: *\nDisallow: /page\nAllow: /page\n')
        self.assertTrue(rules.allowed('Googlebot', 'https://example.test/page'))

    def test_robots_most_specific_group_only(self):
        rules = s.Robots('User-agent: *\nDisallow: /\n\nUser-agent: GPTBot\nUser-agent: Googlebot\nDisallow: /private\n')
        self.assertTrue(rules.allowed('Googlebot', 'https://example.test/a'))
        self.assertFalse(rules.allowed('googlebot', 'https://example.test/private/x'))
        self.assertFalse(rules.allowed('Bingbot', 'https://example.test/a'))

    def test_robots_groups_merge_and_empty_disallow_allows(self):
        rules = s.Robots('User-agent: A\nDisallow: /x\n\nUser-agent: A\nDisallow: /y\n\nUser-agent: B\nDisallow:\n')
        self.assertFalse(rules.allowed('A', 'https://example.test/y'))
        self.assertFalse(rules.allowed('A', 'https://example.test/x'))
        self.assertTrue(rules.allowed('B', 'https://example.test/x'))

    def test_robots_collects_sitemaps_and_ignores_unknown_lines(self):
        rules = s.Robots('\ufeff# hi\nSitemap: https://example.test/sitemap-index.xml # note\nContent-Signal: search=yes\nUser-agent: *\nAllow: /\n')
        self.assertEqual(rules.sitemaps, ['https://example.test/sitemap-index.xml'])
        self.assertTrue(rules.allowed('Googlebot', URL))

    def test_allow_rule_keeps_sitemap_url_from_being_flagged(self):
        def override(url, ua):
            if url.endswith('/robots.txt'):
                return 200, {}, b'User-agent: *\nDisallow: /\nAllow: /$\n'
        code, output, snap = self.run_live_mock(override)
        self.assertEqual(code, 0)
        self.assertNotIn('blocked by robots.txt', output)

    def discover(self, routes):
        def fetch(url, ua=s.UA, limit=None):
            return routes.get(url, (404, {}, b''))
        with patch.object(s, 'fetch', side_effect=fetch):
            return s.discover_sitemaps('https://example.test/')

    def test_sitemap_discovery_prefers_robots_declaration(self):
        robots = (200, {}, b'Sitemap: https://example.test/sitemap-index.xml\nUser-agent: *\nAllow: /\n')
        self.assertEqual(self.discover({URL + 'robots.txt': robots}), [URL + 'sitemap-index.xml'])

    def test_sitemap_discovery_falls_back_to_astro_default(self):
        found = self.discover({URL + 'robots.txt': (200, {}, b'User-agent: *\nAllow: /\n'),
                               URL + 'sitemap-index.xml': (200, {}, b'<sitemapindex/>')})
        self.assertEqual(found, [URL + 'sitemap-index.xml'])

    def test_sitemap_discovery_stops_on_failure_not_404(self):
        found = self.discover({URL + 'sitemap.xml': (503, {}, b'down'),
                               URL + 'sitemap-index.xml': (200, {}, b'<sitemapindex/>')})
        self.assertEqual(found, [URL + 'sitemap.xml'])

    def test_sitemap_discovery_reports_conventional_path_when_nothing_found(self):
        self.assertEqual(self.discover({}), [URL + 'sitemap.xml'])

    def test_cross_host_sitemap_declaration_is_noted(self):
        def override(url, ua):
            if url.endswith('/robots.txt'):
                return 200, {}, b'Sitemap: https://app.vercel.app/sitemap.xml\nUser-agent: *\nAllow: /\n'
        code, output, _ = self.run_live_mock(override)
        self.assertEqual(code, 0)
        self.assertIn('robots.txt declares a sitemap on another host', output)

    # Bot protection
    def test_challenge_headers_and_titles_are_recognized(self):
        self.assertIn('x-vercel-mitigated', s.challenge({'x-vercel-mitigated': 'challenge'}, b''))
        self.assertIn('cf-mitigated', s.challenge({'cf-mitigated': 'challenge'}, b''))
        for title in (b'Just a moment...', b'Vercel Security Checkpoint', b'Attention Required! | Cloudflare'):
            with self.subTest(title=title):
                self.assertTrue(s.challenge({}, b'<html><title>' + title + b'</title></html>'))
        self.assertIsNone(s.challenge({'x-vercel-id': 'abc', 'cf-ray': '1'}, page().encode()))

    # Wrong-host URLs
    def test_preview_and_local_urls_are_flagged(self):
        h = page('<meta property="og:url" content="https://proj-abc.vercel.app/">'
                 '<link rel="alternate" hreflang="de" href="http://localhost:3000/de">'
                 '<script type="application/ld+json">{"@type":"Organization","logo":"https://x.netlify.app/l.png"}</script>')
        problems = ' | '.join(s.analyze(URL, 200, h, self_url=URL)['problems'])
        for text in ('wrong host: og:url -> proj-abc.vercel.app', 'wrong host: hreflang -> localhost', 'wrong host: JSON-LD -> x.netlify.app'):
            self.assertIn(text, problems)

    def test_own_platform_host_and_cdn_are_not_wrong_hosts(self):
        h = page().replace('https://example.test/card.png', 'https://res.cloudinary.com/x/card.png')
        self.assertFalse([p for p in s.analyze(URL, 200, h, self_url=URL)['problems'] if p.startswith('wrong host')])
        app = 'https://acme.vercel.app/'
        h = page(canonical=f'<link rel="canonical" href="{app}">').replace('https://example.test/card.png', app + 'card.png')
        self.assertFalse([p for p in s.analyze(app, 200, h, self_url=app)['problems'] if p.startswith('wrong host')])

    def test_wrong_host_found_without_self_url_in_built_folder(self):
        h = page().replace('https://example.test/card.png', 'http://localhost:3000/card.png')
        self.assertTrue(any(p.startswith('wrong host: og:image') for p in s.analyze('index.html', 200, h)['problems']))

    def test_sitemap_loc_on_preview_host_is_noted(self):
        def override(url, ua):
            if url.endswith('/sitemap.xml'):
                return 200, {}, b'<urlset><url><loc>https://proj.vercel.app/a</loc></url></urlset>'
        code, output, _ = self.run_live_mock(override)
        self.assertIn('sitemap URLs on proj.vercel.app', output)

    # Snippet limits
    def test_snippet_limits_from_meta_and_header(self):
        for extra, headers, text in [('<meta name="robots" content="nosnippet">', None, 'nosnippet'),
                                     ('<meta name="googlebot" content="max-snippet:0">', None, 'max-snippet:0'),
                                     ('<meta name="robots" content="max-image-preview:none">', None, 'max-image-preview:none'),
                                     ('', {'X-Robots-Tag': 'nosnippet'}, 'nosnippet'),
                                     ('', {'X-Robots-Tag': 'googlebot: max-snippet: 0'}, 'max-snippet:0')]:
            with self.subTest(extra=extra, headers=headers):
                self.assertTrue(any(p.startswith('snippet restricted') and text in p for p in row(page(extra), headers)['problems']))

    def test_harmless_snippet_settings_and_other_bots_pass(self):
        for extra, headers in [('<meta name="robots" content="max-snippet:-1, max-image-preview:large">', None),
                               ('<meta name="robots" content="max-snippet:120">', None),
                               ('', {'X-Robots-Tag': 'bingbot: nosnippet'})]:
            with self.subTest(extra=extra, headers=headers):
                self.assertFalse([p for p in row(page(extra), headers)['problems'] if p.startswith('snippet restricted')])

    def test_data_nosnippet_is_a_review_item(self):
        r = row(page(body='<div data-nosnippet>x</div><span data-nosnippet>y</span>'))
        self.assertTrue(any(p.startswith('data-nosnippet on 2') for p in r['problems']))

    # Share-card fields
    def test_og_url_must_match_canonical(self):
        r = s.analyze(URL + 'a', 200, page('<meta property="og:url" content="https://example.test/">',
                                           canonical=f'<link rel="canonical" href="{URL}a">'), self_url=URL + 'a')
        self.assertTrue(any(p.startswith('og:url') and 'canonical' in p for p in r['problems']), r['problems'])

    def test_matching_og_url_passes(self):
        self.assertEqual(row(page(f'<meta property="og:url" content="{URL}">'))['problems'], [])

    def test_missing_twitter_card_is_reported(self):
        h = page().replace('<meta name="twitter:card" content="summary_large_image">', '')
        self.assertTrue(any(p.startswith('no twitter:card') for p in row(h)['problems']))

    def test_twitter_card_as_property_counts(self):
        h = page().replace('name="twitter:card"', 'property="twitter:card"')
        self.assertEqual(row(h)['problems'], [])

    # Thin raw HTML
    def test_client_rendered_body_is_flagged(self):
        h = page().replace('<p>' + 'word ' * 40 + '</p>', '<div id="root"></div><script>' + 'var a = 1; ' * 200 + '</script>'
                           '<noscript>' + 'enable javascript please ' * 20 + '</noscript>')
        self.assertTrue(any(p.startswith('thin HTML body') for p in row(h)['problems']), row(h)['problems'])

    def test_thin_body_not_reported_on_noindex_pages(self):
        h = page('<meta name="robots" content="noindex">').replace('word ' * 40, '')
        self.assertFalse([p for p in row(h)['problems'] if p.startswith('thin HTML body')])

    # Internal links
    def site(self, pages):
        """Override serving a sitemap of the given {path: html} pages."""
        def override(url, ua):
            if url.endswith('/sitemap.xml'):
                return 200, {}, ('<urlset>' + ''.join(f'<url><loc>{URL}{p}</loc></url>' for p in pages) + '</urlset>').encode()
            path = url[len(URL):]
            if path in pages:
                return 200, {}, page(canonical=f'<link rel="canonical" href="{url}">', body=pages[path]).encode()
        return override

    def test_broken_and_redirecting_links_are_noted(self):
        base = self.site({'': '<a href="/gone">a</a><a href="old#frag">b</a><a href="/ok">c</a>', 'ok': '<a href="/">home</a>'})
        def override(url, ua):
            if url == URL + 'gone':
                return 404, {}, b''
            if url == URL + 'old':
                return 308, {'Location': URL + 'new'}, b''
            return base(url, ua)
        code, output, snap = self.run_live_mock(override)
        self.assertEqual(code, 0)
        self.assertIn(f'internal link {URL}gone is 404', output)
        self.assertIn(f'internal link {URL}old redirects (308 -> {URL}new)', output)
        self.assertNotIn(f'internal link {URL}ok', output)
        self.assertTrue(any('is 404' in note for note in snap['site_notes']))

    def test_external_and_special_links_are_not_fetched(self):
        code, _, _ = self.run_live_mock(self.site({'': '<a href="https://other.test/x">a</a><a href="mailto:a@example.test">b</a>'
                                                       '<a href="tel:1">c</a><a href="javascript:void(0)">d</a><a href="#top">e</a>'}))
        self.assertEqual(code, 0)
        self.assertNotIn('https://other.test/x', self.requested)

    def test_gated_links_are_not_judged(self):
        base = self.site({'': '<a href="/account">a</a>'})
        code, output, _ = self.run_live_mock(lambda url, ua: (401, {}, b'') if url == URL + 'account' else base(url, ua))
        self.assertEqual(code, 0)
        self.assertNotIn(f'internal link {URL}account is', output)
        self.assertIn('1 internal link(s) answered 401/403', output)

    def test_failed_link_fetch_is_incomplete_not_broken(self):
        for status in (503, 429, 'ERR URLError', 'ERR bot challenge (cf-mitigated: challenge)'):
            base = self.site({'': '<a href="/x">a</a>'})
            with self.subTest(status=status):
                code, output, _ = self.run_live_mock(lambda url, ua: (status, {}, b'') if url == URL + 'x' else base(url, ua))
                self.assertEqual(code, 2)
                self.assertIn(f'internal link {URL}x not measured ({status})', output)

    def test_robots_disallowed_links_are_not_fetched(self):
        base = self.site({'': '<a href="/private/x">a</a>'})
        def override(url, ua):
            if url.endswith('/robots.txt'):
                return 200, {}, b'User-agent: *\nDisallow: /private/\n'
            return base(url, ua)
        code, _, _ = self.run_live_mock(override)
        self.assertEqual(code, 0)
        self.assertNotIn(URL + 'private/x', self.requested)

    def test_orphan_sitemap_pages_are_noted(self):
        code, output, _ = self.run_live_mock(self.site({'': '<a href="/linked">a</a>', 'linked': '<a href="/">a</a>', 'orphan': '<a href="/orphan">self</a>'}))
        self.assertEqual(code, 0)
        self.assertIn('no link from another sitemap page', output)
        self.assertIn(URL + 'orphan', output)
        self.assertNotIn(f'{URL}linked,', output)

    def test_trailing_slash_link_still_counts_as_inbound(self):
        _, output, _ = self.run_live_mock(self.site({'': '<a href="/a/">a</a>', 'a': '<a href="/">a</a>'}))
        self.assertNotIn('no link from another sitemap page', output)

    def test_no_links_anywhere_is_not_reported_as_orphans(self):
        _, output, _ = self.run_live_mock(self.site({'': '', 'a': ''}))
        self.assertNotIn('no link from another sitemap page', output)

    def test_link_fetches_are_capped_and_disclosed(self):
        links = {'': '<a href="/x1">a</a><a href="/x2">b</a><a href="/x3">c</a>'}
        with patch.object(s, 'MAX_LINK_CHECKS', 1):
            code, output, _ = self.run_live_mock(self.site(links))
        self.assertEqual(code, 0)
        self.assertIn('2 internal link target(s) not checked', output)
        self.assertEqual(sum(1 for u in self.requested if u.startswith(URL + 'x')), 1)

    def test_quick_skips_link_fetches_and_says_so(self):
        code, output, _ = self.run_live_mock(self.site({'': '<a href="/x">a</a>'}), '--quick')
        self.assertEqual(code, 0)
        self.assertNotIn(URL + 'x', self.requested)
        self.assertIn('internal link status not checked (--quick)', output)

    def test_same_save_compare_path_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'index.html').write_text(page())
            dest = root / 'snapshot.json'; dest.write_text('{"original": true}')
            code, _ = cli([str(root), '--save', str(dest), '--compare', str(dest)])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(dest.read_text()), {'original': True})


if __name__ == '__main__':
    unittest.main()
