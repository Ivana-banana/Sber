"""Count what a run actually spent at the endpoint, whatever happens to the run.

Development tool. `agent/` never imports it and it never enters the archive.

Why it exists. Tokens are the second ranking criterion, and the agent reports its own
total on the last line it prints -- which a run killed at its deadline never reaches.
That is not a rare case: in E009 most rows were killed, so the metric was missing from
exactly the runs that were burning it. A counter in front of the endpoint does not care
how the run ends.

It is also an independent check on the agent's own accounting. Two numbers that agree
are two measurements; one number is a claim.

The same position makes it the only place the run's actions are visible without adding
anything to the archive. E009 could say that eleven runs were killed with nothing on
disk, and nothing at all about what they spent those steps doing -- the agent prints two
status lines and no trace. Every request carries the whole conversation and every reply
carries the action, so recording the last exchange of each call reconstructs the
trajectory from outside the agent.

Only stdlib. Deliberately non-streaming: the agent asks for whole completions, so the
proxy can buffer a response, read its `usage`, and pass the bytes on untouched. If the
client ever starts streaming, the counters here would silently stop seeing usage, so
that case is counted separately rather than ignored.
"""

from __future__ import annotations

import argparse
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

EXCHANGES_PATH = "/__exchanges"
COUNTERS_PATH = "/__counters"
RESET_PATH = "/__reset"
MAX_EXCHANGES = 200
EXCHANGE_CHARS = 2000
"""Bounds on what one task's trace may hold. A run is capped at 30 steps, so 200 leaves
room for retries and protocol fallbacks without letting a wedged run fill memory."""

UPSTREAM_TIMEOUT_S = 900.0
"""Long, because one CPU-bound completion legitimately takes minutes here. A proxy that
gives up first would be recorded as an endpoint failure the endpoint never had."""


class Counters:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.epoch = 0
        """Bumped on every reset, so a reply outliving its task cannot be charged to
        the next one.

        A task killed at its deadline leaves a request in flight: the agent process is
        gone, but the model keeps generating and the proxy's thread is still waiting.
        That answer arrives seconds later, after the harness has already reset the
        counters for the following task, and without this it is counted -- and traced --
        as if the next task had made it. Measured on E011: two calls attributed to
        `fix-redirect-scheme` were byte-for-byte the answer `fix-cache-key-scope` was
        still working on when it was killed.
        """
        self._zero()

    def _zero(self) -> None:
        self.exchanges: list[dict[str, object]] = []
        self.requests = 0
        self.late_responses = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.total_tokens = 0
        self.responses_without_usage = 0
        self.upstream_errors = 0

    def reset(self) -> None:
        with self.lock:
            self.epoch += 1
            self._zero()

    def record(self, payload: bytes, epoch: int) -> None:
        with self.lock:
            if epoch != self.epoch:
                self.late_responses += 1
                return
            self.requests += 1
            try:
                usage = json.loads(payload).get("usage") or {}
            except (ValueError, AttributeError):
                self.responses_without_usage += 1
                return
            if not usage:
                self.responses_without_usage += 1
                return
            self.remember(payload)
            self.input_tokens += int(usage.get("prompt_tokens", 0))
            self.output_tokens += int(usage.get("completion_tokens", 0))
            self.total_tokens += int(
                usage.get("total_tokens", 0)
                or usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
            )

    def remember(self, payload: bytes) -> None:
        """Keep the model's reply -- the action it chose -- for the run's trace.

        Called with the lock already held. Only the assistant's own text: the request
        carries the system prompt and the whole observation history on every call, and
        storing that would be storing the same kilobytes tens of times per task.
        """
        if len(self.exchanges) >= MAX_EXCHANGES:
            return
        try:
            choices = json.loads(payload).get("choices") or []
            message = choices[0].get("message", {}) if choices else {}
        except (ValueError, AttributeError, IndexError, KeyError):
            return
        self.exchanges.append(
            {
                "call": self.requests,
                "content": str(message.get("content") or "")[:EXCHANGE_CHARS],
                "tool_calls": str(message.get("tool_calls") or "")[:EXCHANGE_CHARS],
            }
        )

    def error(self, epoch: int) -> None:
        with self.lock:
            if epoch != self.epoch:
                self.late_responses += 1
                return
            self.requests += 1
            self.upstream_errors += 1

    def snapshot(self) -> dict[str, int]:
        with self.lock:
            return {
                "requests": self.requests,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "total_tokens": self.total_tokens,
                "responses_without_usage": self.responses_without_usage,
                "upstream_errors": self.upstream_errors,
                "late_responses": self.late_responses,
            }


COUNTERS = Counters()


class Handler(BaseHTTPRequestHandler):
    upstream = "http://sber-model:11434"
    protocol_version = "HTTP/1.1"

    def log_message(self, *_: object) -> None:
        """Silence. The harness reads counters, not an access log."""

    def _json(self, payload: dict[str, object], status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        if self.path == COUNTERS_PATH:
            self._json(COUNTERS.snapshot())
            return
        if self.path == EXCHANGES_PATH:
            with COUNTERS.lock:
                self._json({"exchanges": list(COUNTERS.exchanges)})
            return
        self._forward(b"")

    def do_POST(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        if self.path == RESET_PATH:
            COUNTERS.reset()
            self._json({"reset": True})
            return
        length = int(self.headers.get("Content-Length") or 0)
        self._forward(self.rfile.read(length) if length else b"")

    def _forward(self, body: bytes) -> None:
        with COUNTERS.lock:
            epoch = COUNTERS.epoch
        headers = {
            key: value
            for key, value in self.headers.items()
            # Hop-by-hop and length headers are ours to set, not the client's to carry.
            if key.lower() not in {"host", "content-length", "connection", "accept-encoding"}
        }
        request = urllib.request.Request(
            f"{self.upstream}{self.path}",
            data=body or None,
            headers=headers,
            method=self.command,
        )
        try:
            with urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT_S) as response:
                payload = response.read()
                status = response.status
                content_type = response.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as exc:
            # An HTTP error is still an answer from the model server, and the agent's
            # ladder reacts to its status code. Pass it through unchanged.
            payload = exc.read()
            status = exc.code
            content_type = exc.headers.get("Content-Type", "application/json")
            COUNTERS.error(epoch)
        except OSError as exc:
            COUNTERS.error(epoch)
            self._json({"error": f"upstream unreachable: {exc}"}, status=502)
            return
        else:
            COUNTERS.record(payload, epoch)

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--upstream", default="http://sber-model:11434")
    args = parser.parse_args()

    Handler.upstream = args.upstream.rstrip("/")
    server = ThreadingHTTPServer((args.listen, args.port), Handler)
    server.daemon_threads = True
    print(f"counting proxy: {args.listen}:{args.port} -> {Handler.upstream}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
