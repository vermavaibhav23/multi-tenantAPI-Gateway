import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


SERVICE_NAME = os.environ.get('BACKEND_SERVICE_NAME', 'dummy-backend-service')
PORT = int(os.environ.get('BACKEND_PORT', '8000'))


class DummyBackendHandler(BaseHTTPRequestHandler):
    def _send_response(self):
        parsed_url = urlparse(self.path)
        payload = {
            'response': 'success',
            'backend_service': SERVICE_NAME,
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
        self.wfile.write(body)

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
