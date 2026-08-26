"""Minimal OpenAI-compatible endpoint used to give the agent a REAL HTTP peer.

Not a model. It scripts replies so the run is deterministic, while everything on the
agent side -- sockets, timeouts, retries, the fallback ladder, the tool calls, the
verifier -- is the production path.
"""
import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

START = time.monotonic()
STATE = {"n": 0}
LOCK = threading.Lock()

def escape_actions(outside):
    """Two ways out of the workspace, so the run says which one the code stops.

    `outside` is a directory of the run harness, never a real system path: the
    probe must be able to fail and leave the artefact somewhere harmless.
    """
    return [
        {"tool": "write_file", "path": "../escape_write.txt", "content": "escaped via write_file\n"},
        {"tool": "run_shell", "command": "echo escaped via run_shell > %s/escape_shell.txt && echo wrote-outside-workspace" % outside},
        {"tool": "run_shell", "command": "cat %s/escape_shell.txt 2>/dev/null || echo absent" % outside},
        {"tool": "finish", "path": "calc.py", "verify": "", "summary": "escape probe"},
    ]

ACTIONS = [
    {"tool": "list_dir", "path": "."},
    {"tool": "read_range", "path": "calc.py", "start_line": 1},
    {"tool": "write_file", "path": "calc.py", "content": "def add(a, b):\n    return a + b\n"},
    {"tool": "finish", "path": "calc.py", "verify": "python3 check.py", "summary": "add returned a difference; fixed the sign"},
]


def log(msg):
    print("[stub %7.2fs] %s" % (time.monotonic() - START, msg), flush=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, status, obj):
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        with LOCK:
            STATE["n"] += 1
            n = STATE["n"]
        shape = "tools" if "tools" in body else (
            body.get("response_format", {}).get("type", "plain")
        )
        log("request #%d path=%s shape=%s messages=%d" % (n, self.path, shape, len(body.get("messages", []))))

        mode = self.server.mode

        if mode == "hang":
            log("request #%d: holding the connection open, never replying" % n)
            time.sleep(3600)
            return

        if mode == "shape" and shape == "tools":
            log("request #%d: refusing native tools with 400" % n)
            self._send(400, {"error": {"message": "tools parameter is not supported by this build"}})
            return

        table = escape_actions(self.server.escape_dir) if mode == "escape" else ACTIONS
        action = table[min(n - 1, len(table) - 1)] if mode != "shape" else table[min(n - 2, len(table) - 1)]
        arguments = json.dumps(action)
        log("request #%d -> %s" % (n, action["tool"]))

        if shape == "tools":
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_%d" % n, "type": "function",
                     "function": {"name": "emit_action", "arguments": arguments}}
                ],
            }
        else:
            message = {"role": "assistant", "content": arguments}

        self._send(200, {
            "id": "stub-%d" % n,
            "object": "chat.completion",
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100 + n, "completion_tokens": 20 + n,
                      "total_tokens": 120 + 2 * n},
        })


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--mode", choices=["script", "hang", "shape", "escape"], default="script")
    ap.add_argument("--escape-dir", default="/root/airi",
                    help="directory outside the workspace the escape probe aims at")
    args = ap.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.mode = args.mode
    server.escape_dir = args.escape_dir
    server.daemon_threads = True
    log("listening on 127.0.0.1:%d mode=%s" % (args.port, args.mode))
    server.serve_forever()


if __name__ == "__main__":
    main()
