"""End-to-end checks against an isolated local HTTP fixture, no internet."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from fixture_server import make_server

SWEEP = Path(__file__).resolve().parents[1] / 'scripts/sweep.py'


class FixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server(0)
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def run_sweep(self, path, *args):
        return subprocess.run([sys.executable, str(SWEEP), self.base + path, *args], capture_output=True, text=True, timeout=30)

    def test_all_seeded_faults_and_clean_controls(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'snapshot.json'
            result = self.run_sweep('/sitemap.xml', '--save', str(dest))
            self.assertEqual(result.returncode, 0, result.stderr)
            rows = json.loads(dest.read_text())['pages']
            for path in ('/ok', '/array-type', '/reverse-canonical'):
                self.assertEqual(rows[self.base + path]['problems'], [], path)
            faults = {'/body-canonical': 'canonical in <body>', '/temp-redirect': '307 temporary',
                      '/client-redirect': 'client-side redirect', '/header-noindex': 'noindex',
                      '/canon-to-dead': 'is 404', '/blocked': 'blocked by robots.txt',
                      '/http-schema': 'http://schema.org', '/big': '>2 MB',
                      '/ua-streamed': 'in <body> for browser', '/bad-image': '800x400'}
            for path, text in faults.items():
                self.assertTrue(any(text in p for p in rows[self.base + path]['problems']), path)
            self.assertIn('lastmod dates are in the future', result.stdout)
            self.assertIn('every sitemap lastmod is identical', result.stdout)

    def test_header_noindex_drift_cli_exit_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'before.json'
            result = self.run_sweep('/sitemap.xml', '--save', str(dest))
            self.assertEqual(result.returncode, 0)
            before = json.loads(dest.read_text())
            target = before['pages'][self.base + '/header-noindex']
            target['noindex'] = False
            target['x_robots_tag'] = ''
            for check in target['ua_checks'].values():
                check['noindex'] = False
            dest.write_text(json.dumps(before))
            result = self.run_sweep('/sitemap.xml', '--compare', str(dest))
            self.assertEqual(result.returncode, 1)
            self.assertIn('noindex added', result.stdout)

    def test_clean_comparison_and_nested_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'new' / 'before.json'
            result = self.run_sweep('/clean.xml', '--save', str(dest))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('with problems: 0', result.stdout)
            result = self.run_sweep('/clean.xml', '--compare', str(dest))
            self.assertEqual(result.returncode, 0)
            self.assertIn('no changes', result.stdout)

    def test_child_sitemap_failure_preserves_saved_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'before.json'; dest.write_text('original')
            result = self.run_sweep('/bad-index.xml', '--save', str(dest))
            self.assertEqual(result.returncode, 2)
            self.assertIn('INCOMPLETE', result.stderr)
            self.assertIn('503', result.stderr)
            self.assertEqual(dest.read_text(), 'original')

    def test_ua_outage_cli_exit_two(self):
        result = self.run_sweep('/ua-failure.xml')
        self.assertEqual(result.returncode, 2)
        self.assertIn('browser placement not measured (503)', result.stdout)


if __name__ == '__main__':
    unittest.main()
