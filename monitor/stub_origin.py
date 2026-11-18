"""Synthetic stand-in for the Cloudflare edge, Access and the ACQOS app.

Used by tests and by the workflow's `synthetic` mode. It never contacts
production. Modes model the failures the monitor must tell apart:

  healthy       app health ok
  tunnel_down   edge up, authenticated route returns Cloudflare 530
  origin_down   edge up, authenticated route returns Cloudflare 502 (HTML)
  app_error     app returns 503 JSON
  db_down       app answers; database component fails
  edge_down     every request returns 522 (edge cannot serve the site)
  login_down    Access login page returns 500
  blocked       edge security rule rejects the client (error 1010)
  blocked_empty the same block with an empty body

Credentials: CF-Access-Client-Id/Secret must equal `cf-id`/`cf-secret`, and
the bearer token must equal TOKEN; otherwise the stub answers the way
Access (302 to login) or the ACQOS gateway (401 JSON) would.
"""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

TOKEN = 'synthetic-monitor-token-' + 'x' * 40


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, status, body=b'', content_type='text/html', headers=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def login_url(self):
        return 'http://localhost:%d/cdn-cgi/access/login' % self.server.server_address[1]

    def do_POST(self):
        if self.path.startswith('/__mode/'):
            self.server.mode = self.path.rsplit('/', 1)[1]
            return self.send(204)
        self.send(404)

    def do_GET(self):
        mode = self.server.mode
        if mode == 'blocked_empty':
            return self.send(403)
        if mode == 'blocked':
            return self.send(403, b'error code: 1010', 'text/plain; charset=UTF-8')
        if mode == 'edge_down':
            return self.send(522, b'<html>Connection timed out</html>')
        if self.path.startswith('/cdn-cgi/access/login'):
            return self.send(500 if mode == 'login_down' else 200, b'<html>Sign in</html>')
        if self.path == '/agent-api/health':
            if (self.headers.get('CF-Access-Client-Id') != 'cf-id'
                    or self.headers.get('CF-Access-Client-Secret') != 'cf-secret'):
                return self.send(302, headers={'Location': self.login_url()})
            if mode == 'tunnel_down':
                return self.send(530, b'<html>Error 1033</html>')
            if mode == 'origin_down':
                return self.send(502, b'<html>Bad gateway</html>')
            if self.headers.get('Authorization') != 'Bearer ' + TOKEN:
                return self.send(401, json.dumps({'error': {'message': 'An agent bearer token is required.'}}).encode(),
                                 'application/json')
            if mode == 'app_error':
                return self.send(503, b'{"error":{"code":"agent_unavailable"}}', 'application/json')
            database = 'fail' if mode == 'db_down' else 'ok'
            body = {'status': 'down' if database == 'fail' else 'ok', 'checkedAt': '2026-01-01T00:00:00Z',
                    'components': {'database': database, 'directory': 'ok', 'worker': 'not_configured'},
                    'cached': False}
            return self.send(200, json.dumps(body).encode(), 'application/json')
        return self.send(302, headers={'Location': self.login_url() + '?redirect_url=%2F'})


def start(mode='healthy', port=0):
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.mode = mode
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--mode', default='healthy')
    args = parser.parse_args()
    server = start(args.mode, args.port)
    print('stub on %d' % server.server_address[1], flush=True)
    threading.Event().wait()
