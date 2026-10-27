# A coding-agent task: the Service that broke its own pod

A hand-built evaluation task for coding agents, with a grader that rejects
fixes which look right but aren't. It comes from a real failure I hit
deploying vLLM on single-node k3s for
[financial-agent](https://github.com/schen9999/financial-agent)
(fix in commit `4032e73`).

## The task

An agent gets `task/`: four Kubernetes manifests, a copy of the model
server's entrypoint, a crash log, and a short user-style prompt
([task/PROMPT.md](task/PROMPT.md)). The model server is in
CrashLoopBackOff with:

```
ValueError: invalid literal for int() with base 10: 'tcp://10.43.0.2:8000'
```

The same image and arguments work under `docker run`.

## The root cause

The model server reads an optional `VLLM_PORT` environment variable as the
base port for its internal sockets. Nobody set it. Kubernetes did: for
every Service in a namespace, the kubelet injects Docker-links-style
variables into every pod, named after the Service. A Service called `vllm`
produces `VLLM_PORT=tcp://<cluster-ip>:8000`, which the server then tries
to parse as an integer.

## Why it is hard for an agent

The error points at the server's code, and the fixes that look obvious
fail in ways the crash log doesn't show:

| Edit | Crash gone? | Graded | Why |
|---|---|---|---|
| `enableServiceLinks: false` on the pod spec | yes | pass | removes the injected variables; the fix that shipped |
| Rename the Service and update the client URL | yes | pass | no `VLLM_` prefix left |
| `unset VLLM_PORT` in a shell wrapper, or `env -u VLLM_PORT` | yes | pass | clears the variable before the server starts |
| Set `VLLM_PORT` to an unused number | yes | pass | works, but pins internal ports; brittle |
| Set `VLLM_PORT: "8000"` | no | fail | collides with the API port: `Address already in use` |
| Rename the Service only | yes | fail | the `api` deployment still calls `http://vllm.models.svc:8000` |
| Delete the Service | yes | fail | same: the client has nothing to reach |
| Rename the Service and its selector | yes | fail | the client resolves, but the Service routes to no pod |
| Patch `server/model_server.py` | n/a | fail | that code ships inside an upstream image the user can't rebuild |

A fix has to keep the server's contract with its caller, which is the
part that lives outside the error message.

## The grader

[grader/grade.py](grader/grade.py) needs no cluster. It reads the agent's
edited manifests and:

1. Simulates the environment the kubelet gives the container
   ([grader/k8s_env.py](grader/k8s_env.py)): service-link variables for
   every Service in the namespace (skipped under `enableServiceLinks:
   false`), then `envFrom`, then the container's own `env`, then shell
   `unset`/`export`/`env -u` edits in a wrapper command.
2. Runs a pristine copy of the server's port resolution
   ([grader/upstream_server.py](grader/upstream_server.py)) against that
   environment, so edits to the agent-visible copy have no effect.
3. Checks three things: the server starts, it still serves its API on the
   container's `http` port 8000, and the client's `LOCAL_MODEL_URL` resolves
   to a Service in the namespace that routes to that port.

[grader/test_grader.py](grader/test_grader.py) runs every row of the table
above as a test case:

```
cd grader && python -m pytest -q     # 11 passed
python grader/grade.py --task-dir task   # the unedited task: FAIL
```

## Running an agent on it

1. Copy `task/` to a fresh directory and `git init` it, so the agent's diff
   is easy to inspect. Do not give the agent access to `grader/` or this
   README.
2. Start the agent in that directory with the contents of `PROMPT.md` as
   its first message. It may read files and edit manifests; there is no
   cluster to apply to.
3. Grade: `python grader/grade.py --task-dir <that directory>`.
4. Record the result, the agent's stated root cause, and the number of
   turns below.

## Results

| Agent | Model | Date | Graded | Root cause stated correctly | Turns | Notes |
|---|---|---|---|---|---|---|
| Claude Code 2.1.312 (`-p`, file tools + shell) | claude-sonnet-5-5 | 2026-10-27 | PASS | yes | 4 | `enableServiceLinks: false`; ~16 s, $0.086 |
| Claude Code 2.1.312 (`-p`, file tools + shell) | claude-sonnet-5-5 | 2026-10-27 | PASS | yes | 4 | same fix; ~20 s, $0.087 |
| Claude Code 2.1.312 (`-p`, file tools + shell) | claude-sonnet-5-5 | 2026-10-27 | PASS | yes | 5 | same fix; ~15 s, $0.088 |

**What the runs show: this version is too easy.** All three runs passed
in under 20 seconds with one edit. All three read `server/model_server.py`
on their second call, and that file hands over the answer: its docstring
explains what `VLLM_PORT` means, and the port-collision check that makes
`VLLM_PORT: "8000"` fail is right there in the code. Every run named the
collision and the client URL as reasons to avoid the other fixes, so the
grader's traps were never tested. A harder version (v2) would:

- drop `server/` entirely, since a user debugging an upstream image has
  the log, not the source;
- reproduce the real incident's three stacked failures, where each fix
  revealed the next: `vllm serve` passed as container `args` to an image
  whose entrypoint is already the API server ("unrecognized arguments:
  serve"), then `VLLM_PORT`, then no `/dev/shm`. The agent
  sees only the first error, and the grader requires all three fixed.

**v2 is built: [v2/](v2/README.md).** No server source, all three stacked
failures, only the first one visible. Three Claude Code runs: 2 pass, 1
fails. The miss was `VLLM_PORT`, the one failure with no textual clue.

## Limits

- The server is a stub modeled on vLLM's port handling, not vLLM. The
  real incident was vLLM v0.10.2; some vLLM releases add a hint about
  Kubernetes service discovery to this error, and this task uses the bare
  parse error.
- The grader simulates the kubelet's variable injection; it does not run a
  cluster. It covers the wrapper forms in the table, and a creative
  wrapper it can't parse fails rather than passes.
- One task is an anecdote, not a benchmark.
