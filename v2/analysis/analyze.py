"""Grade a batch of agent runs on the v2 task and summarize per model.

Usage:
  python v2/analysis/analyze.py --runs-dir DIR [--json-out FILE]

DIR holds one subdirectory per model; each contains run<N>/ (the agent's
working copy of task/) and run<N>.jsonl (the Claude Code stream-json
transcript). For each run it records the grader verdict and, separately
for each of the three failures, whether the agent's manifests fix it:

  args   the server's argv parses (stage 1)
  port   the container's VLLM_PORT, if any, is an integer that isn't 8000
  shm    /dev/shm is at least the stub's 512 MiB threshold

The per-failure flags are scored independently of each other, so a run
that missed only VLLM_PORT shows args and shm as fixed.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "grader"))
import grade as G  # noqa: E402
from k8s_env import container_env  # noqa: E402


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def per_failure(task_dir: Path) -> dict[str, bool | None]:
    stub = G._load_stub()
    objs = G.load_objects(task_dir)
    services = G._find(objs, "Service", lambda o: o["metadata"].get("namespace", G.NS) == G.NS)
    cms = {o["metadata"]["name"]: (o.get("data") or {}) for o in G._find(objs, "ConfigMap")}
    deps = G._find(objs, "Deployment", lambda o: (o["spec"]["template"]["metadata"].get("labels") or {}).get("app") == "vllm")
    if len(deps) != 1:
        return {"args": None, "port": None, "shm": None}
    pod = deps[0]["spec"]["template"]["spec"]
    c = pod["containers"][0]
    out: dict[str, bool | None] = {}
    try:
        env = container_env(pod, c, services, cms)
        launcher, argv, env = G.process_start(c, env)
    except Exception:
        return {"args": False, "port": None, "shm": G.shm_bytes(pod, c) >= stub.MIN_SHM_BYTES}
    try:
        stub.start(launcher, argv, {}, 1 << 40)
        out["args"] = True
    except stub.StartupError:
        out["args"] = False
    v = env.get("VLLM_PORT")
    out["port"] = v is None or (v.isdigit() and int(v) != 8000)
    out["shm"] = G.shm_bytes(pod, c) >= stub.MIN_SHM_BYTES
    return out


def transcript_stats(path: Path) -> dict:
    res: dict = {}
    for line in path.read_text().splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") == "result":
            res = d
    return {"turns": res.get("num_turns"), "cost_usd": res.get("total_cost_usd"),
            "seconds": round((res.get("duration_ms") or 0) / 1000, 1),
            "completed": bool(res) and not res.get("is_error", False)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs-dir", required=True, type=Path)
    ap.add_argument("--json-out", type=Path)
    a = ap.parse_args()
    rows = []
    for mdir in sorted(p for p in a.runs_dir.iterdir() if p.is_dir()):
        for run in sorted((p for p in mdir.iterdir() if p.is_dir()), key=lambda p: int(p.name[3:])):
            g = G.grade(run)
            row = {"model": mdir.name, "run": run.name, "pass": g["pass"],
                   "first_failing_stage": g["first_failing_stage"],
                   "failed_checks": [c["check"] for c in g["checks"] if not c["pass"]]}
            row.update({f"fixed_{k}": v for k, v in per_failure(run).items()})
            t = mdir / f"{run.name}.jsonl"
            row.update(transcript_stats(t) if t.exists() else {})
            rows.append(row)

    print("| Model | n | Pass | Pass rate (95% CI) | Fixed args | Fixed VLLM_PORT | Fixed /dev/shm | Median turns | Median s | Mean $ |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for m in sorted({r["model"] for r in rows}):
        rs = [r for r in rows if r["model"] == m]
        n = len(rs)
        k = sum(r["pass"] for r in rs)
        lo, hi = wilson(k, n)
        def cnt(key):
            return sum(1 for r in rs if r.get(key))
        def med(key):
            v = sorted(r[key] for r in rs if r.get(key) is not None)
            return v[len(v) // 2] if v else None
        costs = [r["cost_usd"] for r in rs if r.get("cost_usd") is not None]
        mean_cost = sum(costs) / len(costs) if costs else float("nan")
        print(f"| {m} | {n} | {k} | {k/n:.0%} ({lo:.0%}–{hi:.0%}) | {cnt('fixed_args')}/{n} | "
              f"{cnt('fixed_port')}/{n} | {cnt('fixed_shm')}/{n} | {med('turns')} | {med('seconds')} | {mean_cost:.3f} |")

    fails = [r for r in rows if not r["pass"]]
    if fails:
        print("\nFailed runs:")
        for r in fails:
            missed = [k[6:] for k in ("fixed_args", "fixed_port", "fixed_shm") if r.get(k) is False]
            print(f"  {r['model']} {r['run']}: first failing stage {r['first_failing_stage']}, "
                  f"failed checks {r['failed_checks']}, missed {missed}, completed={r.get('completed')}")
    if a.json_out:
        a.json_out.write_text(json.dumps(rows, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
