"""Stub of the model-server image's startup, used only by the grader.

Modeled on vllm/vllm-openai:v0.10.2. The image's ENTRYPOINT is
`python3 -m vllm.entrypoints.openai.api_server`; `vllm serve` is the
equivalent CLI. Startup runs in stages, and each stage's failure hides
the ones after it, as in the real incident:

  1. argument parsing   (entrypoint vs. `vllm serve` passed as args)
  2. internal port      (VLLM_PORT, which a Service named `vllm` sets)
  3. engine start       (shared memory: Docker's default 64 MiB /dev/shm)
"""
from __future__ import annotations

import argparse

DEFAULT_API_PORT = 8000
DEFAULT_INTERNAL_PORT = 51000
MIN_SHM_BYTES = 512 * 1024**2


class StartupError(Exception):
    def __init__(self, stage: str, log: str):
        super().__init__(log)
        self.stage = stage
        self.log = log


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # argparse prints and exits; we raise
        raise StartupError("args", f"usage: {self.prog} [options] [model_tag]\n{self.prog}: error: {message}")


def _parser(prog: str, positional_required: bool) -> _Parser:
    p = _Parser(prog=prog, add_help=False)
    p.add_argument("model_tag", nargs=None if positional_required else "?")
    p.add_argument("--model", default=None)
    p.add_argument("--port", type=int, default=DEFAULT_API_PORT)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--served-model-name", default=None)
    p.add_argument("--max-model-len", type=int, default=None)
    p.add_argument("--gpu-memory-utilization", type=float, default=None)
    return p


def start(launcher: str, argv: list[str], env: dict[str, str], shm_bytes: int) -> dict:
    """launcher: 'entrypoint' (python -m ...api_server) or 'vllm-serve'."""
    if launcher == "vllm-serve":
        args = _parser("vllm serve", positional_required=True).parse_args(argv)
    else:
        args = _parser("api_server.py", positional_required=False).parse_args(argv)
    model = args.model or args.model_tag
    if not model:
        raise StartupError("args", "api_server.py: error: no model specified")

    if "VLLM_PORT" in env:
        try:
            internal = int(env["VLLM_PORT"])
        except ValueError:
            raise StartupError("vllm_port", (
                'Traceback (most recent call last):\n'
                '  File "/usr/local/lib/python3.12/dist-packages/vllm/envs.py", line 1, in get_vllm_port\n'
                f"ValueError: invalid literal for int() with base 10: '{env['VLLM_PORT']}'"))
    else:
        internal = DEFAULT_INTERNAL_PORT
    if internal == args.port:
        raise StartupError("vllm_port", f"OSError: [Errno 98] Address already in use: ('0.0.0.0', {internal})")

    if shm_bytes < MIN_SHM_BYTES:
        raise StartupError("shm", (
            "INFO: Starting engine core with model=" + model + "\n"
            "Bus error (core dumped)"))

    return {"model": model, "served_model_name": args.served_model_name or model,
            "api_port": args.port, "internal_port": internal}
