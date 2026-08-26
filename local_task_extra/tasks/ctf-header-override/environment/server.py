from http.server import BaseHTTPRequestHandler, HTTPServer
FLAG = "FLAG{header_scope_7b3c}"
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def do_GET(self):
        if self.path == '/diagnostics':
            body = ('status=green\n' * 800) + 'operator-note=not-a-flag\n'
            self.send_response(200); self.end_headers(); self.wfile.write(body.encode()); return
        if self.path == '/vault':
            if self.headers.get('X-Forwarded-Host') == 'ops.local':
                open('/app/issued.flag','w').write(FLAG)
                self.send_response(200); self.end_headers(); self.wfile.write(FLAG.encode()); return
            self.send_response(403); self.end_headers(); self.wfile.write(b'forbidden'); return
        self.send_response(404); self.end_headers()
HTTPServer(('127.0.0.1',9137), Handler).serve_forever()
