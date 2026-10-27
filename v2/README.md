# v2: three stacked failures, no server source

v1 was too easy: every run read the server's source and found the answer
there ([results](../README.md#results)). v2 removes the source and
reproduces the real incident more closely. In the real deployment, three
problems surfaced one after another, each hidden until the previous one
was fixed:

1. **Arguments.** The manifest passes `vllm serve <model> ...` as container
   `args`, but the image's entrypoint is already the API server, so it
   exits with `unrecognized arguments: serve /models/financial-lora`.
2. **`VLLM_PORT`.** The Service named `vllm` makes the kubelet inject
   `VLLM_PORT=tcp://<ip>:8000`, which the server parses as its internal
   port and crashes on (v1's bug).
3. **Shared memory.** The working `docker run` uses `--ipc=host`; a pod
   gets a 64 MiB `/dev/shm` unless the manifest provides more.

In the real incident, (1) and (2) were observed crashes; the `/dev/shm`
volume went in with the same fix (financial-agent commit `4032e73`). The
stub models (3) as a crash ("Bus error") below 512 MiB.

## What the agent gets

[task/](task/): the manifests, the stage-1 crash log only, and a prompt
([task/PROMPT.md](task/PROMPT.md)) that includes the working `docker run`
command and says each rollout costs a GPU node restart, so the fix should
be complete rather than just address the visible error. No server source,
no cluster.

The agent can only see failure 1. Passing requires inferring failure 2
from Kubernetes behavior and failure 3 from the gap between `docker run
--ipc=host` and the pod spec.

## The grader

[grader/grade.py](grader/grade.py) simulates the container start: which
launcher runs (image entrypoint or `vllm serve`), its argv, the injected
environment (service links, `envFrom`, `env`, shell `unset`/`env -u`), and
the `/dev/shm` size (`hostIPC`, or an `emptyDir`/`hostPath` mounted there).
It runs a staged stub ([grader/upstream_server.py](grader/upstream_server.py))
and reports the first stage that fails. A fix passes only if the server
starts, still serves on port 8000, the client's `LOCAL_MODEL_URL` still
routes to it, and the mounted model is served under the client's
`LOCAL_MODEL_NAME`.

[grader/test_grader.py](grader/test_grader.py), 12 cases: each partial fix
fails at exactly the next stage; three different complete fixes pass,
including the one that shipped (`command: [vllm, serve]`); a shell wrapper
and a consistent Service rename pass; dropping `--served-model-name`, a
64Mi `/dev/shm`, and `VLLM_PORT: "8000"` fail.

```
cd v2/grader && python -m pytest -q          # 12 passed
python v2/grader/grade.py --task-dir v2/task  # unedited: fails at stage 'args'
```

## Results

Same setup as v1: Claude Code 2.1.312 in print mode, file tools and a
shell, a fresh git repo containing only `task/`, no access to the grader.

| Run | Model | Date | Graded | Fixed args | Fixed `VLLM_PORT` | Fixed `/dev/shm` | Turns | Time, cost |
|---|---|---|---|---|---|---|---|---|
| 1 | claude-sonnet-5-5 | 2026-10-27 | **FAIL** (stage `vllm_port`) | yes | **no** | yes (8Gi emptyDir) | 4 | 26 s, $0.097 |
| 2 | claude-sonnet-5-5 | 2026-10-27 | PASS | yes | yes | yes (8Gi emptyDir) | 4 | 34 s, $0.103 |
| 3 | claude-sonnet-5-5 | 2026-10-27 | PASS | yes | yes | yes (8Gi emptyDir) | 3 | 26 s, $0.096 |

Transcripts: [runs/](runs/).

**What the runs show.** All three runs fixed the visible error by matching
the `docker run` line, and all three read `--ipc=host` and added a
`/dev/shm` volume. The split is on `VLLM_PORT`, the one failure with no
textual clue in the task: nothing in the manifests, log or prompt mentions
it, and spotting it requires connecting the Service's name to Kubernetes'
environment injection and to a vLLM setting. Run 1 missed it and would have
crashed on the next rollout, which the prompt said to avoid. Runs 1 and 2
also added `strategy: Recreate` for the single GPU, and run 2 a
`startupProbe`, both reasonable and outside what the grader scores.

Three runs is too few to estimate a pass rate; 2/3 says the task now
discriminates, not how well.

## Limits

- The server is a stub modeled on vLLM v0.10.2's startup, not vLLM; real
  error text differs in detail, and the `/dev/shm` threshold is a modeling
  choice, not a measured vLLM limit.
- The grader simulates the kubelet; it does not run a cluster.
- The prompt's line about getting it right in one go tells the agent to
  look past the visible error. Without it, grading failures the agent
  cannot observe would be unfair; with it, the task partly tests whether
  the agent follows that instruction.
