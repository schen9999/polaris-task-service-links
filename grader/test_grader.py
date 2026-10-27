"""The grader must accept every real fix and reject every tempting wrong one.

Each case copies task/ to a temp dir, applies one edit an agent might make,
and grades it. Run: python -m pytest grader/ -q
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

import pytest
import yaml

from grade import grade

ROOT = Path(__file__).resolve().parent.parent
TASK = ROOT / "task"


def _edit(path: Path, fn: Callable[[dict], None]) -> None:
    doc = yaml.safe_load(path.read_text())
    fn(doc)
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


def _container(doc: dict) -> dict:
    return doc["spec"]["template"]["spec"]["containers"][0]


def original(d: Path) -> None:
    pass


def enable_service_links_false(d: Path) -> None:
    _edit(d / "k8s/vllm-deployment.yaml",
          lambda doc: doc["spec"]["template"]["spec"].__setitem__("enableServiceLinks", False))


def rename_service_and_client(d: Path) -> None:
    _edit(d / "k8s/vllm-service.yaml", lambda doc: doc["metadata"].__setitem__("name", "model-server"))
    _edit(d / "k8s/app-config.yaml",
          lambda doc: doc["data"].__setitem__("LOCAL_MODEL_URL", "http://model-server.models.svc:8000"))


def shell_unset(d: Path) -> None:
    def fn(doc):
        c = _container(doc)
        args = " ".join(c.pop("args"))
        c["command"] = ["/bin/sh", "-c"]
        c["args"] = [f"unset VLLM_PORT; exec python3 -m vllm.entrypoints.openai.api_server {args}"]
    _edit(d / "k8s/vllm-deployment.yaml", fn)


def exec_env_u(d: Path) -> None:
    def fn(doc):
        c = _container(doc)
        c["command"] = ["env", "-u", "VLLM_PORT", "python3", "-m", "vllm.entrypoints.openai.api_server"]
    _edit(d / "k8s/vllm-deployment.yaml", fn)


def vllm_port_other_number(d: Path) -> None:
    _edit(d / "k8s/vllm-deployment.yaml",
          lambda doc: _container(doc)["env"].append({"name": "VLLM_PORT", "value": "51000"}))


def vllm_port_same_as_api(d: Path) -> None:
    _edit(d / "k8s/vllm-deployment.yaml",
          lambda doc: _container(doc)["env"].append({"name": "VLLM_PORT", "value": "8000"}))


def rename_service_only(d: Path) -> None:
    _edit(d / "k8s/vllm-service.yaml", lambda doc: doc["metadata"].__setitem__("name", "model-server"))


def delete_service(d: Path) -> None:
    (d / "k8s/vllm-service.yaml").unlink()


def patch_server_code(d: Path) -> None:
    p = d / "server/model_server.py"
    p.write_text(p.read_text().replace(
        'return int(env["VLLM_PORT"])',
        'try:\n        return int(env["VLLM_PORT"])\n    except ValueError:\n        return None'))


def selector_broken(d: Path) -> None:
    def fn(doc):
        doc["metadata"]["name"] = "model-server"
        doc["spec"]["selector"] = {"app": "model-server"}
    _edit(d / "k8s/vllm-service.yaml", fn)
    _edit(d / "k8s/app-config.yaml",
          lambda doc: doc["data"].__setitem__("LOCAL_MODEL_URL", "http://model-server.models.svc:8000"))


CASES = [
    (original, False),
    (enable_service_links_false, True),
    (rename_service_and_client, True),
    (shell_unset, True),
    (exec_env_u, True),
    (vllm_port_other_number, True),   # works; brittle, noted in the README
    (vllm_port_same_as_api, False),   # the obvious override: collides with the API port
    (rename_service_only, False),     # crash gone, client broken
    (delete_service, False),          # crash gone, client broken
    (patch_server_code, False),       # edits code that ships in the upstream image
    (selector_broken, False),         # renamed consistently but routes nowhere
]


@pytest.mark.parametrize("edit,expected", CASES, ids=[c[0].__name__ for c in CASES])
def test_case(tmp_path: Path, edit, expected: bool) -> None:
    work = tmp_path / "task"
    shutil.copytree(TASK, work)
    edit(work)
    result = grade(work)
    assert result["pass"] is expected, result["checks"]
