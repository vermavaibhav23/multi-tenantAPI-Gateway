import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


SERVICE_NAME = os.environ.get('BACKEND_SERVICE_NAME', 'dummy-backend-service')
PORT = int(os.environ.get('BACKEND_PORT', '8000'))


class DummyBackendHandler(BaseHTTPRequestHandler):
    service_name = SERVICE_NAME

    def _send_response(self):
        # Drain the opaque body before closing HTTP/1.0 connections. Leaving it
        # unread can cause a TCP reset and discard the echo response on Windows.
        try:
            remaining = max(0, int(self.headers.get('Content-Length', '0')))
        except ValueError:
            remaining = 0
        while remaining:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)
        parsed_url = urlparse(self.path)
        payload = {
            'response': 'success',
            'backend_service': self.service_name,
            'application_id': self._application_id(),
            'method': self.command,
            'path': parsed_url.path,
        }
        if parsed_url.query:
            payload['query'] = parsed_url.query

        body = json.dumps(payload).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _application_id(self):
        # Trusted only on the private gateway/backend network, not on public ingress.
        try:
            value = int(self.headers.get('X-GateScale-Application-ID', ''))
            return value if value > 0 else None
        except ValueError:
            return None

    def do_HEAD(self):
        self._send_response()

    def do_OPTIONS(self):
        self._send_response()

    def do_GET(self):
        self._send_response()

    def do_POST(self):
        self._send_response()

    def do_PUT(self):
        self._send_response()

    def do_PATCH(self):
        self._send_response()

    def do_DELETE(self):
        self._send_response()

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    server = ThreadingHTTPServer(('0.0.0.0', PORT), DummyBackendHandler)
    print(f'{SERVICE_NAME} listening on port {PORT}', flush=True)
    server.serve_forever()
