"""Simulate the environment the kubelet gives a container.

Implements Kubernetes' Docker-links-style service environment variables
(pkg/kubelet/envvars): for every Service in the pod's namespace with a
cluster IP, the kubelet injects

    {NAME}_SERVICE_HOST, {NAME}_SERVICE_PORT, {NAME}_SERVICE_PORT_{PORTNAME}
    {NAME}_PORT = {proto}://{ip}:{port}          (first port)
    {NAME}_PORT_{port}_{PROTO}, ..._PROTO, ..._PORT, ..._ADDR

where NAME is the Service name upper-cased with '-' replaced by '_'.
Injection is skipped when the pod spec sets enableServiceLinks: false.
Variables the container declares itself in `env` take precedence.
"""
from __future__ import annotations

import ipaddress
from typing import Any


def _env_name(service_name: str) -> str:
    return service_name.upper().replace("-", "_")


def fake_cluster_ips(services: list[dict[str, Any]]) -> dict[str, str]:
    net = ipaddress.ip_network("10.43.0.0/16")
    hosts = net.hosts()
    next(hosts)  # skip .1, the apiserver's address
    out: dict[str, str] = {}
    for svc in services:
        name = svc["metadata"]["name"]
        ip = (svc.get("spec") or {}).get("clusterIP")
        out[name] = ip if ip else str(next(hosts))
    return out


def service_link_env(services: list[dict[str, Any]]) -> dict[str, str]:
    env: dict[str, str] = {}
    ips = fake_cluster_ips(services)
    for svc in services:
        spec = svc.get("spec") or {}
        if spec.get("clusterIP") == "None":  # headless: no links
            continue
        ports = spec.get("ports") or []
        if not ports:
            continue
        name = _env_name(svc["metadata"]["name"])
        ip = ips[svc["metadata"]["name"]]
        first = ports[0]
        env[f"{name}_SERVICE_HOST"] = ip
        env[f"{name}_SERVICE_PORT"] = str(first["port"])
        for p in ports:
            if p.get("name"):
                env[f"{name}_SERVICE_PORT_{_env_name(p['name'])}"] = str(p["port"])
        proto = (first.get("protocol") or "TCP").lower()
        env[f"{name}_PORT"] = f"{proto}://{ip}:{first['port']}"
        for p in ports:
            pr = (p.get("protocol") or "TCP")
            base = f"{name}_PORT_{p['port']}_{pr.upper()}"
            env[base] = f"{pr.lower()}://{ip}:{p['port']}"
            env[f"{base}_PROTO"] = pr.lower()
            env[f"{base}_PORT"] = str(p["port"])
            env[f"{base}_ADDR"] = ip
    return env


def container_env(pod_spec: dict[str, Any], container: dict[str, Any],
                  services: list[dict[str, Any]],
                  configmaps: dict[str, dict[str, str]]) -> dict[str, str]:
    """Effective environment of one container at process start."""
    env: dict[str, str] = {}
    if pod_spec.get("enableServiceLinks", True) is not False:
        env.update(service_link_env(services))
    for src in container.get("envFrom") or []:
        ref = (src.get("configMapRef") or {}).get("name")
        if ref and ref in configmaps:
            prefix = src.get("prefix", "")
            env.update({prefix + k: str(v) for k, v in configmaps[ref].items()})
    for item in container.get("env") or []:
        if "value" in item:
            env[item["name"]] = str(item["value"])
        elif "valueFrom" in item:
            cm = (item["valueFrom"] or {}).get("configMapKeyRef")
            if cm and cm.get("name") in configmaps:
                val = configmaps[cm["name"]].get(cm.get("key"))
                if val is not None:
                    env[item["name"]] = str(val)
        else:
            env[item["name"]] = ""
    return env
