"""Hidden grader for the service-links task.

Usage: python grader/grade.py --task-dir path/to/agent/workdir [--json]

Reads the agent's edited manifests (k8s/*.yaml), simulates the
environment the kubelet would give the model-server container, and runs
the upstream server's port resolution against it (grader/upstream_server.py,
a pristine copy: edits the agent makes to task/server/ are ignored,
because that code ships inside an upstream image).

A fix passes only if all three hold:
  server_starts       the model server resolves its ports without error
  api_port_unchanged  it still serves its API on the container's http port
  client_reaches_it   the app's LOCAL_MODEL_URL still resolves to a Service
                      in the namespace that routes to that port
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from k8s_env import container_env  # noqa: E402

SHELLS = {"sh", "bash", "/bin/sh", "/bin/bash", "/usr/bin/env sh"}
MODEL_SERVER_LABEL = "vllm"
CLIENT_KEY = "LOCAL_MODEL_URL"


def load_upstream():
    spec = importlib.util.spec_from_file_location("upstream_server", HERE / "upstream_server.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def load_objects(task_dir: Path) -> list[dict[str, Any]]:
    objs: list[dict[str, Any]] = []
    for path in sorted((task_dir / "k8s").glob("*.y*ml")):
        if path.name.startswith("kustomization"):
            continue
        for doc in yaml.safe_load_all(path.read_text()):
            if isinstance(doc, dict) and doc.get("kind"):
                objs.append(doc)
    return objs


def _segments(container: dict[str, Any]) -> list[list[str]]:
    """Split the container's process into shell-like segments of tokens."""
    command = [str(c) for c in container.get("command") or []]
    args = [str(a) for a in container.get("args") or []]
    if not command:
        return [args]
    if command[0] in SHELLS or (len(command) > 1 and command[:2] == ["/usr/bin/env", "sh"]):
        full = command + args
        if "-c" not in full:
            return [full]
        script = full[full.index("-c") + 1] if full.index("-c") + 1 < len(full) else ""
        parts = re.split(r"\n|;|&&", script)
        return [shlex.split(p) for p in parts if p.strip()]
    return [command + args]


def process_start(container: dict[str, Any], env: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    """Apply shell env edits and return (server argv, final env)."""
    env = dict(env)
    argv: list[str] | None = None
    for toks in _segments(container):
        i = 0
        while i < len(toks):
            t = toks[i]
            if t == "unset":
                for name in toks[i + 1:]:
                    env.pop(name, None)
                break
            if t == "export":
                for assign in toks[i + 1:]:
                    if "=" in assign:
                        k, v = assign.split("=", 1)
                        env[k] = v
                break
            if t in {"exec", "env"}:
                i += 1
                continue
            if t == "-u" and i + 1 < len(toks):
                env.pop(toks[i + 1], None)
                i += 2
                continue
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", t):
                k, v = t.split("=", 1)
                env[k] = v
                i += 1
                continue
            model_idx = next((j for j in range(i, len(toks)) if toks[j].startswith("/models")), None)
            if model_idx is not None:
                argv = toks[model_idx:]
            break
    if argv is None:
        raise ValueError("could not find the model server's command line (no /models/... argument)")
    return argv, env


def _find(objs, kind, pred=lambda o: True):
    return [o for o in objs if o.get("kind") == kind and pred(o)]


def grade(task_dir: Path) -> dict[str, Any]:
    upstream = load_upstream()
    objs = load_objects(task_dir)
    ns = "models"
    services = _find(objs, "Service", lambda o: o["metadata"].get("namespace", ns) == ns)
    configmaps = {o["metadata"]["name"]: (o.get("data") or {}) for o in _find(objs, "ConfigMap")}
    checks: list[dict[str, Any]] = []

    def record(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "pass": ok, "detail": detail})

    server_deps = _find(objs, "Deployment", lambda o: ((o["spec"]["template"]["metadata"].get("labels") or {}).get("app") == MODEL_SERVER_LABEL))
    if len(server_deps) != 1:
        record("server_starts", False, f"expected one model-server Deployment (app={MODEL_SERVER_LABEL}), found {len(server_deps)}")
        return {"pass": False, "checks": checks}
    dep = server_deps[0]
    pod = dep["spec"]["template"]["spec"]
    pod_labels = dep["spec"]["template"]["metadata"].get("labels") or {}
    container = pod["containers"][0]

    api_port = None
    try:
        env = container_env(pod, container, services, configmaps)
        argv, env = process_start(container, env)
        api_port, internal = upstream.resolve_ports(argv, env)
        record("server_starts", True, f"API port {api_port}, internal port {internal}")
    except Exception as exc:  # the crash the agent must fix, or a new one
        record("server_starts", False, f"{type(exc).__name__}: {exc}")

    named_ports = {p.get("name"): p["containerPort"] for p in container.get("ports") or [] if p.get("name")}
    all_ports = [p["containerPort"] for p in container.get("ports") or []]
    http_port = named_ports.get("http", all_ports[0] if all_ports else None)
    if api_port is None:
        record("api_port_unchanged", False, "server did not start")
    else:
        ok = api_port == http_port == 8000
        record("api_port_unchanged", ok, f"server API port {api_port}, container http port {http_port} (expected 8000)")

    url = None
    for d in _find(objs, "Deployment", lambda o: o is not dep):
        for c in d["spec"]["template"]["spec"]["containers"]:
            e = container_env(d["spec"]["template"]["spec"], c, [], configmaps)
            if CLIENT_KEY in e:
                url = e[CLIENT_KEY]
    if not url:
        record("client_reaches_it", False, f"no client container sets {CLIENT_KEY}")
    else:
        u = urlparse(url)
        labels = (u.hostname or "").split(".")
        svc_name = labels[0]
        svc_ns = labels[1] if len(labels) > 1 else ns
        port = u.port or (443 if u.scheme == "https" else 80)
        svc = next((s for s in services if s["metadata"]["name"] == svc_name), None)
        if svc_ns != ns or svc is None:
            record("client_reaches_it", False, f"{url}: no Service '{svc_name}' in namespace '{svc_ns}'")
        else:
            sel = (svc.get("spec") or {}).get("selector") or {}
            sp = next((p for p in svc["spec"].get("ports") or [] if p["port"] == port), None)
            if not sel or any(pod_labels.get(k) != v for k, v in sel.items()):
                record("client_reaches_it", False, f"Service '{svc_name}' selector {sel} does not match the model-server pod")
            elif sp is None:
                record("client_reaches_it", False, f"Service '{svc_name}' exposes no port {port}")
            else:
                tp = sp.get("targetPort", sp["port"])
                target = named_ports.get(tp) if isinstance(tp, str) else tp
                ok = target is not None and target == http_port
                record("client_reaches_it", ok, f"{url} -> Service {svc_name}:{port} -> pod port {target} (container http port {http_port})")

    return {"pass": all(c["pass"] for c in checks), "checks": checks}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-dir", required=True, type=Path)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    result = grade(a.task_dir)
    if a.json:
        print(json.dumps(result, indent=2))
    else:
        for c in result["checks"]:
            print(f"[{'PASS' if c['pass'] else 'FAIL'}] {c['check']}: {c['detail']}")
        print("RESULT:", "PASS" if result["pass"] else "FAIL")
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
