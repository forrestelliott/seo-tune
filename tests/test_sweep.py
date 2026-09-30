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


def page(extra="", canonical=None):
    return ('<html><head><title>Example page</title>'
            '<meta name="description" content="' + 'd' * 130 + '">'
            + (canonical or f'<link rel="canonical" href="{URL}">')
            + '<meta property="og:title" content="Example page">'
            '<meta property="og:description" content="Example description">'
            '<meta property="og:image" content="https://example.test/card.png">'
            '<meta name="twitter:image" content="https://example.test/card.png">'
            + extra + '</head><body><h1>Example</h1></body></html>')


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

    def run_live_mock(self, override):
        from fixture_server import IMAGES
        def fetch(url, ua=s.UA):
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
            code, output = cli([URL, '--save', str(dest)])
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

    def test_same_save_compare_path_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'index.html').write_text(page())
            dest = root / 'snapshot.json'; dest.write_text('{"original": true}')
            code, _ = cli([str(root), '--save', str(dest), '--compare', str(dest)])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(dest.read_text()), {'original': True})


if __name__ == '__main__':
    unittest.main()
