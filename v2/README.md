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
discriminates, not how well. The 20-run comparison below does that.

## 20 runs per model (2026-10-27)

Same setup, 20 independent runs each on Sonnet, Opus and Haiku, 8 at a
time. Graded with [analysis/analyze.py](analysis/analyze.py), which also
scores each of the three failures separately. Pass rates with Wilson 95%
intervals; tests are two-sided Fisher exact.

| Model | n | Pass | Pass rate (95% CI) | Fixed args | Fixed `VLLM_PORT` | Fixed `/dev/shm` | Median turns | Median time | Mean cost |
|---|---|---|---|---|---|---|---|---|---|
| claude-sonnet-5-5 | 20 | 9 | 45% (26–66%) | 20/20 | 9/20 | 20/20 | 4 | 28 s | $0.097 |
| claude-opus-5-5 | 20 | 8 | 40% (22–61%) | 20/20 | 8/20 | 20/20 | 4 | 41 s | $0.236 |
| claude-haiku-5-5 | 19 | 0 | 0% (0–17%) | 19/19 | 0/19 | 19/19 | 7 | 33 s | $0.009 |

Total cost $6.84. Results per run: [runs-n20/results.json](runs-n20/results.json);
each run's diff and compressed transcript are in [runs-n20/](runs-n20/).

**Findings**

- **Every failure in all 59 runs is the same one: `VLLM_PORT`.** All 59
  fixed the visible argument error and all 59 caught the `/dev/shm` gap
  from `--ipc=host`. The failure with a textual clue is solved every time;
  the one that needs a Kubernetes behavior nobody mentions is solved less
  than half the time by the best model.
- **Opus did not beat Sonnet here:** 8/20 vs 9/20, p = 1.00, overlapping
  intervals, at 2.4 times the cost. Both beat Haiku (0/19; p = 0.0012
  Sonnet, p = 0.0033 Opus).
- **Every pass used the same fix,** `enableServiceLinks: false`. No run
  renamed the Service, unset the variable in a wrapper, or overrode
  `VLLM_PORT`, so those grader paths were never exercised by a real run.
- **Agents fixed more than the grader scores.** 28 of 59 runs added
  `strategy: Recreate` for the single GPU, and 15 a `startupProbe`. One Opus run
  (run5) moved the app config into a kustomize `configMapGenerator` and
  appended `/v1` to the client URL, an unrequested change that would
  break a client which adds `/v1` itself; the grader checks only host and
  port, so it doesn't catch that.

**Validity notes**

- **One run excluded (Haiku run18).** It concluded its working directory
  was wrong, searched `/home/claude`, found this repository, read v1's
  server source and edited v1's manifests instead of its own copy. That is
  an environment failure, not a model result; its transcript is kept in
  [runs-n20/excluded/](runs-n20/excluded/). Its edit to v1 was reverted.
  Future runs should use a container or a separate user with no access to
  the repository.
- **Three Sonnet runs (2, 10, 12) ran `find / -name crashloop.log`,** which
  listed other runs' paths and this repository's path. Their transcripts
  show they read nothing outside their own directory, and all three
  passed. Excluding them, Sonnet is 6/17; the comparison with Opus doesn't
  change.
- **Grader fix found by a run.** Opus run5's `configMapGenerator` was
  invisible to the original grader, which read every YAML file and ignored
  `kustomization.yaml`. The grader now applies only the files kustomize
  lists, reads `configMapGenerator` literals, and marks a kustomization it
  can't interpret as ungradable instead of scoring it. Two test cases were
  added (14 total). No verdict changed: run5 had failed on `VLLM_PORT`.

## Limits

- The server is a stub modeled on vLLM v0.10.2's startup, not vLLM; real
  error text differs in detail, and the `/dev/shm` threshold is a modeling
  choice, not a measured vLLM limit.
- The grader simulates the kubelet; it does not run a cluster. Its
  service-link injection matches a real kubelet on kind for this task's
  Service shape ([validation/kind-service-links.md](validation/kind-service-links.md));
  the launcher, shell and `/dev/shm` logic are not validated against a
  cluster.
- The prompt's line about getting it right in one go tells the agent to
  look past the visible error. Without it, grading failures the agent
  cannot observe would be unfair; with it, the task partly tests whether
  the agent follows that instruction.
