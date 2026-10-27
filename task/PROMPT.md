Our model server pod in the `models` namespace won't come up. It's in
CrashLoopBackOff; `kubectl get pods` and the container log are in
`logs/crashloop.log`.

The same image and the same arguments work fine with `docker run` on the
GPU box, so the model, the weights, and the GPU are not the problem. The
manifests are in `k8s/` (applied with `kubectl apply -k k8s/`).
`server/model_server.py` is a copy of the image's entrypoint for reference;
the image itself comes from upstream and we can't rebuild it.

Please fix the manifests so the model server starts and the `api`
deployment can use it again. Explain the root cause in a few sentences.
