# Grader check: service-link env vars on a real kubelet

Date: 2026-10-27. kind v1.36.1 (kindest/node:v1.36.1), WSL2.

Setup: namespace `models`, `kubectl create service clusterip vllm --tcp=8000:8000`,
then a busybox pod printing `env | grep ^VLLM_`.

## Default pod (service links on)

    VLLM_PORT=tcp://10.96.11.73:8000
    VLLM_PORT_8000_TCP=tcp://10.96.11.73:8000
    VLLM_PORT_8000_TCP_ADDR=10.96.11.73
    VLLM_PORT_8000_TCP_PORT=8000
    VLLM_PORT_8000_TCP_PROTO=tcp
    VLLM_SERVICE_HOST=10.96.11.73
    VLLM_SERVICE_PORT=8000
    VLLM_SERVICE_PORT_8000_8000=8000

## Pod with `enableServiceLinks: false`

No `VLLM_*` variables.

## Result

`grader/k8s_env.py`'s `service_link_env`, given the same Service and cluster IP,
produces the same 8 variables, identical line for line. With
`enableServiceLinks: false` it produces none. The kubelet behavior the grader
simulates matches a real kubelet for this case.
