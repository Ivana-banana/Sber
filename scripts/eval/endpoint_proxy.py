"""Forward one TCP port from a docker bridge gateway to a host-local endpoint.

Development tool. Nothing in `agent/` imports it and it never enters the archive.

Why it exists. The evaluation contract gives the agent one reachable service --
`OPENAI_BASE_URL` -- and no internet. Reproducing that locally has one obstacle: a
local model server usually listens on `127.0.0.1` only, which a container cannot
reach, and the two obvious workarounds each break something that matters.
`--network host` would let the task's own service bind host ports, so a task could
collide with the machine or with the next repeat; rebinding the model server to
`0.0.0.0` is a change to a system service outside this repository.

So the bridge itself carries the endpoint: the eval network is created `--internal`
(no route off the machine), and this proxy sits on its gateway address, which is the
host. The container then has exactly one thing it can talk to, which is the property
the real run has.

Only stdlib, because the repository forbids adding runtime dependencies for anything
an agent run touches, and a dev tool that needs `pip install` is a tool that will not
run inside a clean checkout.
"""

from __future__ import annotations

import argparse
import socket
import socketserver
import sys
import threading

BUFFER_BYTES = 65536
CONNECT_TIMEOUT_S = 10.0


class Handler(socketserver.BaseRequestHandler):
    """One client connection, spliced onto one upstream connection."""

    upstream: tuple[str, int]

    def handle(self) -> None:
        try:
            remote = socket.create_connection(self.upstream, timeout=CONNECT_TIMEOUT_S)
        except OSError as exc:  # upstream down: close the client rather than hang it
            print(f"proxy: upstream {self.upstream} unreachable: {exc}", file=sys.stderr)
            return
        # No timeout on the spliced sockets on purpose. A slow CPU-bound model can
        # take minutes to answer one request, and a proxy that gives up mid-generation
        # would show up in the metrics as an endpoint failure that the real endpoint
        # never had.
        remote.settimeout(None)
        self.request.settimeout(None)
        with remote:
            forward = threading.Thread(
                target=self._pump, args=(self.request, remote), daemon=True
            )
            forward.start()
            self._pump(remote, self.request)
            forward.join(timeout=1.0)

    @staticmethod
    def _pump(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                chunk = src.recv(BUFFER_BYTES)
                if not chunk:
                    break
                dst.sendall(chunk)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default="172.28.0.1", help="address to bind on the host")
    parser.add_argument("--port", type=int, default=11434)
    parser.add_argument("--upstream-host", default="127.0.0.1")
    parser.add_argument("--upstream-port", type=int, default=11434)
    args = parser.parse_args()

    Handler.upstream = (args.upstream_host, args.upstream_port)
    with Server((args.listen, args.port), Handler) as server:
        print(
            f"proxy: {args.listen}:{args.port} -> "
            f"{args.upstream_host}:{args.upstream_port}",
            flush=True,
        )
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
