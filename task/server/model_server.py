"""Reference copy of the model-server image's entrypoint.

This mirrors the upstream image (an OpenAI-compatible inference server).
The image is pulled from upstream as-is: changes to this file do not
change what runs in the cluster.

Port handling, as in the upstream server:
  --port         the HTTP API port clients connect to (default 8000)
  VLLM_PORT      optional env var: base port for the server's internal
                 RPC/communication sockets. If unset, a free port is chosen.
"""
from __future__ import annotations

import argparse
import os
import sys

DEFAULT_API_PORT = 8000
DEFAULT_INTERNAL_PORT = 51000  # stands in for "pick a free ephemeral port"


def get_vllm_port(env: dict[str, str]) -> int | None:
    """Return the internal base port from VLLM_PORT, or None if unset."""
    if "VLLM_PORT" not in env:
        return None
    return int(env["VLLM_PORT"])


def resolve_ports(argv: list[str], env: dict[str, str]) -> tuple[int, int]:
    """Return (api_port, internal_port), raising on any startup error."""
    parser = argparse.ArgumentParser(prog="model_server")
    parser.add_argument("model")
    parser.add_argument("--port", type=int, default=DEFAULT_API_PORT)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--served-model-name", default=None)
    parser.add_argument("--max-model-len", type=int, default=None)
    parser.add_argument("--gpu-memory-utilization", type=float, default=None)
    args, _unknown = parser.parse_known_args(argv)

    internal = get_vllm_port(env)
    if internal is None:
        internal = DEFAULT_INTERNAL_PORT
    if internal == args.port:
        raise OSError(98, f"[Errno 98] Address already in use: ('0.0.0.0', {internal})")
    return args.port, internal


def main() -> None:
    api_port, internal_port = resolve_ports(sys.argv[1:], dict(os.environ))
    print(f"INFO: internal communication on port {internal_port}")
    print(f"INFO: Uvicorn running on http://0.0.0.0:{api_port}")


if __name__ == "__main__":
    main()
