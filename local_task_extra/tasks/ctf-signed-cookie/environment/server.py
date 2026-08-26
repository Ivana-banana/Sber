import base64, hashlib, hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
SECRET = b'calendar-2026'
FLAG = 'FLAG{cookie_math_81d2}'
def sig(value): return hmac.new(SECRET, value.encode(), hashlib.sha256).hexdigest()
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*_): pass
    def do_GET(self):
        if self.path == '/hint':
            self.send_response(200); self.end_headers(); self.wfile.write(b'format: role|hex_hmac_sha256; secret tracks the annual calendar') ; return
        if self.path == '/desk':
            cookie=self.headers.get('Cookie','')
            token=cookie.removeprefix('session=')
            try: value, given=token.split('|',1)
            except ValueError: value,given='',''
            if hmac.compare_digest(sig(value),given) and value=='role=auditor':
                open('/app/issued.flag','w').write(FLAG)
                self.send_response(200); self.end_headers(); self.wfile.write(FLAG.encode()); return
            self.send_response(403); self.end_headers(); self.wfile.write(b'no session') ; return
        self.send_response(404); self.end_headers()
HTTPServer(('127.0.0.1',9138),Handler).serve_forever()
