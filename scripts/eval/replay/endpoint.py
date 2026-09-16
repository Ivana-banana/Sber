"""Replay endpoint: answers each chat call with the next scripted action for the task."""
import json, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCRIPTS = json.load(open(sys.argv[1]))
COUNT: dict[str, int] = {}
LOG = open(sys.argv[2], "a")
LOCK = threading.Lock()

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        body = json.dumps({"data": [{"id": "replay"}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        text = json.dumps(req.get("messages", []), ensure_ascii=False)
        key = next((k for k in SCRIPTS if k in text), None)
        with LOCK:
            if key is None:
                action = {"tool": "finish", "path": "/nonexistent", "summary": "no script"}
            else:
                i = COUNT.get(key, 0); COUNT[key] = i + 1
                steps = SCRIPTS[key]
                action = steps[min(i, len(steps) - 1)]
            last = req.get("messages", [{}])[-1].get("content", "")
            import re as _re
            blob = json.dumps(action, ensure_ascii=False)
            if "$READY" in blob or "$KILL" in blob:
                ready = _re.findall(r"Готовая команда: `(.+?)` —", text)
                kill = _re.findall(r"`kill (\d+)`", text)
                action = json.loads(json.dumps(action, ensure_ascii=False).replace("$READY", ready[-1] if ready else "true").replace("$KILL", kill[-1] if kill else "0"))
            LOG.write(json.dumps({"key": key, "n": COUNT.get(key), "sent": action, "last": str(last)[-600:]}, ensure_ascii=False) + "\n"); LOG.flush()
        content = json.dumps(action, ensure_ascii=False)
        tools = req.get("tools")
        msg = {"role": "assistant", "content": content}
        if tools:
            msg = {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": tools[0]["function"]["name"], "arguments": content}}]}
        body = json.dumps({"id": "x", "object": "chat.completion", "model": req.get("model"), "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}], "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)

ThreadingHTTPServer(("0.0.0.0", 8000), H).serve_forever()
