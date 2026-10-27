"""v2 grader: partial fixes fail at the next stage; every complete fix passes.

Run: cd v2/grader && python -m pytest -q
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from grade import grade

TASK = Path(__file__).resolve().parent.parent / "task"
DEP = "k8s/vllm-deployment.yaml"


def _load(d: Path, rel: str) -> dict:
    return yaml.safe_load((d / rel).read_text())


def _save(d: Path, rel: str, doc: dict) -> None:
    (d / rel).write_text(yaml.safe_dump(doc, sort_keys=False))


def _pod(doc):
    return doc["spec"]["template"]["spec"]


def _c(doc):
    return _pod(doc)["containers"][0]


# Individual fixes -----------------------------------------------------------

def fix_args_drop_serve(doc):        # match the docker run line
    _c(doc)["args"] = _c(doc)["args"][2:]


def fix_args_command(doc):           # the fix that shipped: vllm serve as command
    _c(doc)["command"] = ["vllm", "serve"]
    _c(doc)["args"] = _c(doc)["args"][2:]


def fix_args_model_flag(doc):
    _c(doc)["args"] = ["--model"] + _c(doc)["args"][2:]


def fix_links_off(doc):
    _pod(doc)["enableServiceLinks"] = False


def fix_shm_memory(doc, limit="2Gi"):
    _c(doc).setdefault("volumeMounts", []).append({"name": "shm", "mountPath": "/dev/shm"})
    _pod(doc).setdefault("volumes", []).append({"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": limit}})


def fix_host_ipc(doc):
    _pod(doc)["hostIPC"] = True


def apply(*fns):
    def edit(d: Path) -> None:
        doc = _load(d, DEP)
        for f in fns:
            f(doc)
        _save(d, DEP, doc)
    return edit


def shell_wrapper_all(d: Path) -> None:
    doc = _load(d, DEP)
    rest = " ".join(_c(doc)["args"][2:])
    _c(doc)["command"] = ["/bin/sh", "-c"]
    _c(doc)["args"] = [f"unset VLLM_PORT; exec vllm serve {rest}"]
    fix_shm_memory(doc)
    _save(d, DEP, doc)


def rename_service_everywhere(d: Path) -> None:
    apply(fix_args_drop_serve, fix_host_ipc)(d)
    svc = _load(d, "k8s/vllm-service.yaml")
    svc["metadata"]["name"] = "model-server"
    _save(d, "k8s/vllm-service.yaml", svc)
    cm = _load(d, "k8s/app-config.yaml")
    cm["data"]["LOCAL_MODEL_URL"] = "http://model-server.models.svc:8000"
    _save(d, "k8s/app-config.yaml", cm)


def all_but_served_name(d: Path) -> None:
    def drop_name(doc):
        a = _c(doc)["args"]
        i = a.index("--served-model-name")
        del a[i:i + 2]
    apply(fix_args_drop_serve, fix_links_off, fix_shm_memory, drop_name)(d)


def port_override_8000(d: Path) -> None:
    apply(fix_args_drop_serve, fix_shm_memory,
          lambda doc: _c(doc)["env"].append({"name": "VLLM_PORT", "value": "8000"}))(d)


CASES = [
    ("original", lambda d: None, False, "args"),
    ("args_only", apply(fix_args_drop_serve), False, "vllm_port"),
    ("args_and_links", apply(fix_args_drop_serve, fix_links_off), False, "shm"),
    ("links_and_shm_no_args", apply(fix_links_off, fix_shm_memory), False, "args"),
    ("shm_too_small", apply(fix_args_drop_serve, fix_links_off, lambda doc: fix_shm_memory(doc, "64Mi")), False, "shm"),
    ("all_three_drop_serve", apply(fix_args_drop_serve, fix_links_off, fix_shm_memory), True, None),
    ("all_three_as_shipped", apply(fix_args_command, fix_links_off, fix_shm_memory), True, None),
    ("all_three_model_flag_hostipc", apply(fix_args_model_flag, fix_links_off, fix_host_ipc), True, None),
    ("shell_wrapper_unset", shell_wrapper_all, True, None),
    ("rename_service_everywhere", rename_service_everywhere, True, None),
    ("served_name_dropped", all_but_served_name, False, None),
    ("vllm_port_8000", port_override_8000, False, "vllm_port"),
]


@pytest.mark.parametrize("name,edit,expected,stage", CASES, ids=[c[0] for c in CASES])
def test_case(tmp_path: Path, name, edit, expected, stage) -> None:
    work = tmp_path / "task"
    shutil.copytree(TASK, work)
    edit(work)
    r = grade(work)
    assert r["pass"] is expected, r["checks"]
    assert r["first_failing_stage"] == stage, r
