"""Exercise host transport reporting with fake commands; no network required."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HOSTS = Path(__file__).resolve().parents[1] / 'scripts/hosts.sh'


class HostsTests(unittest.TestCase):
    def test_dns_tls_timeout_and_stale_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            curl = root / 'curl'
            curl.write_text('#!' + sys.executable + '\n' + '''
import sys
from pathlib import Path
args = sys.argv[1:]
url = args[-1]
if 'dev.example.test' in url: sys.exit(60)
if 'beta.example.test' in url: sys.exit(6)
if 'test.example.test' in url: sys.exit(28)
body = '<meta name="robots" content="noindex">' if 'staging.example.test' in url else '<html>indexable</html>'
if '-o' in args:
    dest = args[args.index('-o') + 1]
    if dest != '/dev/null': Path(dest).write_text(body)
if '-D' in args: print('HTTP/1.1 200 OK')
if '-w' in args:
    print('__STATUS__200 ' if '__STATUS__' in args[args.index('-w') + 1] else '200 ')
else: print('User-agent: *\\nAllow: /')
''')
            curl.chmod(0o755)
            python = root / 'python3'
            python.write_text('#!/bin/sh\ncat >/dev/null\nprintf "robots fixture\\n"\n')
            python.chmod(0o755)
            result = subprocess.run(['bash', str(HOSTS), 'example.test'],
                                    env={**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH']},
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = {sub: next(line for line in result.stdout.splitlines() if f'https://{sub}.example.test/' in line)
                     for sub in ('staging', 'dev', 'preview', 'beta', 'test')}
            self.assertIn('noindex (ok)', lines['staging'])
            self.assertIn('not measured (curl exit 60', lines['dev'])
            self.assertIn('DNS resolution failed (not measured)', lines['beta'])
            self.assertIn('not measured (curl exit 28', lines['test'])
            self.assertIn('indexable: PROBLEM', lines['preview'])
            self.assertNotIn('unreachable (fine)', result.stdout)


if __name__ == '__main__':
    unittest.main()
