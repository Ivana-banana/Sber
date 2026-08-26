from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit
FLAG='FLAG{hpp_split_brain_44af}'
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*_): pass
    def do_GET(self):
        parsed=urlsplit(self.path)
        if parsed.path == '/diagnostics':
            body=''.join(f'metric_{i:04d}=ok\n' for i in range(5000))+'flag=FLAG{not_the_proof}\n'
            self.send_response(200); self.end_headers(); self.wfile.write(body.encode()); return
        if parsed.path == '/v1/export':
            values=parse_qs(parsed.query, keep_blank_values=True).get('tier',['guest'])
            if values[0] != 'guest':
                self.send_response(403); self.end_headers(); self.wfile.write(b'first tier rejected'); return
            if values[-1] == 'auditor':
                open('/app/issued.flag','w').write(FLAG)
                self.send_response(200); self.end_headers(); self.wfile.write(FLAG.encode()); return
            self.send_response(403); self.end_headers(); self.wfile.write(b'last tier insufficient'); return
        self.send_response(404); self.end_headers()
HTTPServer(('127.0.0.1',9139),Handler).serve_forever()
