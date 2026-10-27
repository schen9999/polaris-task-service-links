"""Hidden grader for the v2 task (three stacked startup failures).

Usage: python v2/grader/grade.py --task-dir path/to/agent/workdir [--json]

Simulates the model-server container's start from the agent's manifests:
which launcher runs (image ENTRYPOINT or `vllm serve`), its argv, the
environment the kubelet injects (service links, envFrom, env, shell edits),
and the size of /dev/shm. Then runs the staged startup stub
(upstream_server.py) and checks the result against the client's config.

A fix passes only if all four hold:
  server_starts          every startup stage clears
  api_port_unchanged     the API is still served on container port 8000
  client_reaches_it      LOCAL_MODEL_URL resolves to a Service routing to it
  serves_expected_model  the mounted model is served under LOCAL_MODEL_NAME
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

SHELLS = {"sh", "bash", "/bin/sh", "/bin/bash"}
ENTRY_MODULE = "vllm.entrypoints.openai.api_server"
NS = "models"
DOCKER_DEFAULT_SHM = 64 * 1024**2
UNBOUNDED = 1 << 40
_UNITS = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
          "k": 1000, "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4}


def _load_stub():
    spec = importlib.util.spec_from_file_location("upstream_server_v2", HERE / "upstream_server.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def quantity(q: Any) -> int:
    s = str(q).strip()
    m = re.fullmatch(r"([0-9.]+)([A-Za-z]*)", s)
    if not m:
        raise ValueError(f"bad quantity {q!r}")
    return int(float(m.group(1)) * _UNITS.get(m.group(2), 1))


def shm_bytes(pod: dict[str, Any], container: dict[str, Any]) -> int:
    if pod.get("hostIPC") is True:
        return UNBOUNDED
    vols = {v["name"]: v for v in pod.get("volumes") or []}
    for vm in container.get("volumeMounts") or []:
        if vm.get("mountPath", "").rstrip("/") != "/dev/shm":
            continue
        vol = vols.get(vm["name"], {})
        if "emptyDir" in vol:
            limit = (vol["emptyDir"] or {}).get("sizeLimit")
            return quantity(limit) if limit else UNBOUNDED
        if "hostPath" in vol:
            return UNBOUNDED
    return DOCKER_DEFAULT_SHM


def _launch(tokens: list[str], env: dict[str, str]) -> tuple[str, list[str]] | None:
    """Strip exec/env prefixes; return (launcher, argv) if this runs the server."""
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in {"exec", "env"}:
            i += 1
        elif t == "-u" and i + 1 < len(tokens):
            env.pop(tokens[i + 1], None)
            i += 2
        elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", t):
            k, v = t.split("=", 1)
            env[k] = v
            i += 1
        else:
            break
    rest = tokens[i:]
    if rest[:2] == ["vllm", "serve"]:
        return "vllm-serve", rest[2:]
    if ENTRY_MODULE in rest:
        return "entrypoint", rest[rest.index(ENTRY_MODULE) + 1:]
    return None


def process_start(container: dict[str, Any], env: dict[str, str]) -> tuple[str, list[str], dict[str, str]]:
    env = dict(env)
    command = [str(c) for c in container.get("command") or []]
    args = [str(a) for a in container.get("args") or []]
    if not command:  # image ENTRYPOINT receives args
        return "entrypoint", args, env
    if command[0] in SHELLS and "-c" in command + args:
        full = command + args
        script = full[full.index("-c") + 1] if full.index("-c") + 1 < len(full) else ""
        for seg in re.split(r"\n|;|&&", script):
            toks = shlex.split(seg)
            if not toks:
                continue
            if toks[0] == "unset":
                for n in toks[1:]:
                    env.pop(n, None)
                continue
            if toks[0] == "export":
                for a in toks[1:]:
                    if "=" in a:
                        k, v = a.split("=", 1)
                        env[k] = v
                continue
            hit = _launch(toks, env)
            if hit:
                return hit[0], hit[1], env
        raise ValueError("could not find the model server's command in the shell script")
    hit = _launch(command + args, env)
    if hit:
        return hit[0], hit[1], env
    raise ValueError(f"unrecognized container command: {command}")


def _find(objs, kind, pred=lambda o: True):
    return [o for o in objs if o.get("kind") == kind and pred(o)]


def load_objects(task_dir: Path) -> list[dict[str, Any]]:
    """What `kubectl apply -k k8s/` would create, for the kustomize features
    the task needs: `resources` (only listed files are applied) and
    `configMapGenerator` literals. Patches and overlays are not supported;
    a kustomization using them is reported, not silently mis-graded."""
    k8s = task_dir / "k8s"
    kfile = next((p for p in (k8s / "kustomization.yaml", k8s / "kustomization.yml") if p.exists()), None)
    kust = yaml.safe_load(kfile.read_text()) if kfile else {}
    unsupported = [k for k in ("patches", "patchesStrategicMerge", "patchesJson6902", "components", "replacements") if kust.get(k)]
    if unsupported:
        raise ValueError(f"kustomization uses unsupported features: {unsupported}")
    files = [k8s / r for r in kust.get("resources", [])] if kfile else sorted(k8s.glob("*.y*ml"))
    objs: list[dict[str, Any]] = []
    for path in files:
        for doc in yaml.safe_load_all(path.read_text()):
            if isinstance(doc, dict) and doc.get("kind"):
                objs.append(doc)
    for gen in kust.get("configMapGenerator") or []:
        data = {}
        for lit in gen.get("literals") or []:
            k, _, v = str(lit).partition("=")
            data[k] = v
        objs.append({"kind": "ConfigMap", "metadata": {"name": gen["name"], "namespace": kust.get("namespace", NS)}, "data": data})
    return objs


def grade(task_dir: Path) -> dict[str, Any]:
    stub = _load_stub()
    try:
        objs = load_objects(task_dir)
    except ValueError as e:  # flagged for a human, never silently scored
        return {"pass": False, "first_failing_stage": "ungradable",
                "checks": [{"check": "gradable", "pass": False, "detail": str(e)}]}
    services = _find(objs, "Service", lambda o: o["metadata"].get("namespace", NS) == NS)
    configmaps = {o["metadata"]["name"]: (o.get("data") or {}) for o in _find(objs, "ConfigMap")}
    checks: list[dict[str, Any]] = []

    def record(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "pass": ok, "detail": detail})

    deps = _find(objs, "Deployment", lambda o: (o["spec"]["template"]["metadata"].get("labels") or {}).get("app") == "vllm")
    if len(deps) != 1:
        record("server_starts", False, f"expected one model-server Deployment (app=vllm), found {len(deps)}")
        return {"pass": False, "first_failing_stage": None, "checks": checks}
    dep = deps[0]
    pod = dep["spec"]["template"]["spec"]
    pod_labels = dep["spec"]["template"]["metadata"].get("labels") or {}
    container = pod["containers"][0]

    started = None
    stage = None
    try:
        env = container_env(pod, container, services, configmaps)
        launcher, argv, env = process_start(container, env)
        started = stub.start(launcher, argv, env, shm_bytes(pod, container))
        record("server_starts", True, f"all stages cleared ({launcher}); API port {started['api_port']}")
    except stub.StartupError as e:
        stage = e.stage
        record("server_starts", False, f"fails at stage '{e.stage}': {e.log.splitlines()[-1]}")
    except Exception as e:
        stage = "unparsed"
        record("server_starts", False, f"{type(e).__name__}: {e}")

    named = {p.get("name"): p["containerPort"] for p in container.get("ports") or [] if p.get("name")}
    ports = [p["containerPort"] for p in container.get("ports") or []]
    http_port = named.get("http", ports[0] if ports else None)
    if started is None:
        record("api_port_unchanged", False, "server did not start")
    else:
        record("api_port_unchanged", started["api_port"] == http_port == 8000,
               f"server API port {started['api_port']}, container http port {http_port} (expected 8000)")

    client_env: dict[str, str] = {}
    for d in _find(objs, "Deployment", lambda o: o is not dep):
        for c in d["spec"]["template"]["spec"]["containers"]:
            e = container_env(d["spec"]["template"]["spec"], c, [], configmaps)
            if "LOCAL_MODEL_URL" in e:
                client_env = e
    url = client_env.get("LOCAL_MODEL_URL")
    if not url:
        record("client_reaches_it", False, "no client container sets LOCAL_MODEL_URL")
    else:
        u = urlparse(url)
        labels = (u.hostname or "").split(".")
        svc_name, svc_ns = labels[0], (labels[1] if len(labels) > 1 else NS)
        port = u.port or 80
        svc = next((s for s in services if s["metadata"]["name"] == svc_name), None)
        if svc_ns != NS or svc is None:
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
                target = named.get(tp) if isinstance(tp, str) else tp
                record("client_reaches_it", target is not None and target == http_port,
                       f"{url} -> Service {svc_name}:{port} -> pod port {target} (container http port {http_port})")

    want = client_env.get("LOCAL_MODEL_NAME")
    mounts = [vm["mountPath"].rstrip("/") for vm in container.get("volumeMounts") or []]
    if started is None:
        record("serves_expected_model", False, "server did not start")
    else:
        mounted = any(started["model"].rstrip("/") == m for m in mounts)
        ok = mounted and started["served_model_name"] == want
        record("serves_expected_model", ok,
               f"model {started['model']} (mounted: {mounted}), served as '{started['served_model_name']}', client asks for '{want}'")

    return {"pass": all(c["pass"] for c in checks), "first_failing_stage": stage, "checks": checks}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-dir", required=True, type=Path)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    r = grade(a.task_dir)
    if a.json:
        print(json.dumps(r, indent=2))
    else:
        for c in r["checks"]:
            print(f"[{'PASS' if c['pass'] else 'FAIL'}] {c['check']}: {c['detail']}")
        print("RESULT:", "PASS" if r["pass"] else "FAIL")
    return 0 if r["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
