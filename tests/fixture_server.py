"""Local fault fixtures. Run with --port 0 for an available port, or --port 8765."""
import argparse
import http.server
import struct
import zlib

DESC = 'd' * 115
PATHS = ['/ok', '/body-canonical', '/temp-redirect', '/client-redirect',
         '/header-noindex', '/canon-to-dead', '/blocked', '/http-schema', '/big',
         '/ua-streamed', '/bad-image', '/array-type', '/reverse-canonical']


def png(width, height):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    pixels = zlib.compress((b'\0' + b'\x40\x60\x80' * width) * height)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>II5B', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', pixels) + chunk(b'IEND', b''))


IMAGES = {'/img.png': png(1200, 630), '/bad-img.png': png(800, 400)}


def page(base, path, head_extra='', body_extra='', canon=None):
    return (f'<html><head><title>Fixture page {path}</title>'
            f'<meta name="description" content="{DESC}{path}">'
            f'<link rel="canonical" href="{canon or base + path}">'
            f'<meta property="og:title" content="Fixture {path}">'
            f'<meta property="og:description" content="Fixture {path}">'
            f'<meta property="og:image" content="{base}/img.png">'
            f'<meta name="twitter:image" content="{base}/img.png">'
            f'{head_extra}</head><body><h1>Fixture</h1>{body_extra}</body></html>')


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        base = f'http://127.0.0.1:{self.server.server_port}'
        path = self.path
        if path in ('/sitemap.xml', '/clean.xml', '/ua-failure.xml'):
            paths = PATHS if path == '/sitemap.xml' else ['/ok'] if path == '/clean.xml' else ['/ua-failure']
            date = '2099-01-01' if path == '/sitemap.xml' else '2026-01-01'
            body = '<urlset>' + ''.join(f'<url><loc>{base}{p}</loc><lastmod>{date}</lastmod></url>' for p in paths) + '</urlset>'
            return self.send(200, {'Content-Type': 'application/xml'}, body)
        if path == '/bad-index.xml':
            return self.send(200, {}, f'<sitemapindex><sitemap><loc>{base}/unavailable.xml</loc></sitemap></sitemapindex>')
        if path == '/unavailable.xml':
            return self.send(503, {}, 'Unavailable')
        if path == '/robots.txt':
            return self.send(200, {}, 'User-agent: *\nDisallow: /blocked\n')
        if path in IMAGES:
            return self.send(200, {'Content-Type': 'image/png'}, IMAGES[path])
        if path == '/temp-redirect':
            return self.send(307, {'Location': base + '/ok'}, '')
        if path == '/ua-failure' and 'bot' not in self.headers.get('User-Agent', '').lower():
            return self.send(503, {}, 'Browser temporarily unavailable')
        if path not in PATHS + ['/ua-failure']:
            return self.send(404, {}, 'Not found')
        headers = {}
        body = page(base, path)
        if path == '/body-canonical':
            tag = f'<link rel="canonical" href="{base}{path}">'
            body = body.replace(tag, '').replace('</body>', tag + '</body>')
        elif path == '/client-redirect':
            body = page(base, path, body_extra='<script>NEXT_REDIRECT;replace;/ok;307;</script>')
        elif path == '/header-noindex':
            headers['X-Robots-Tag'] = 'noindex'
        elif path == '/canon-to-dead':
            body = page(base, path, canon=base + '/dead')
        elif path == '/http-schema':
            body = page(base, path, head_extra='<script type="application/ld+json">{"@context":"http://schema.org","@type":"Organization"}</script>')
        elif path == '/big':
            body = page(base, path, body_extra='x' * (2 * 1024 * 1024 + 10))
        elif path == '/ua-streamed' and 'bot' not in self.headers.get('User-Agent', '').lower():
            body = body.replace('</head><body>', '').replace('<head>', '<head></head><body>')
        elif path == '/bad-image':
            body = body.replace('/img.png', '/bad-img.png')
        elif path == '/array-type':
            body = page(base, path, head_extra='<script type="application/ld+json">{"@context":"https://schema.org","@type":["Organization","LocalBusiness"]}</script>')
        elif path == '/reverse-canonical':
            body = body.replace(f'rel="canonical" href="{base}{path}"', f'href="{base}{path}" rel="canonical"')
        return self.send(200, headers, body)

    def send(self, status, headers, body):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(status)
        headers = {'Content-Type': 'text/html; charset=utf-8', **headers}
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def make_server(port=0):
    return http.server.ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    with make_server(args.port) as server:
        print(f'Fixture site: http://127.0.0.1:{server.server_port}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
