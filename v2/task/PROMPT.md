Our model server pod in the `models` namespace won't come up. It's in
CrashLoopBackOff; `kubectl get pods` and the container log are in
`logs/crashloop.log`. The manifests are in `k8s/` (applied with
`kubectl apply -k k8s/`).

The same model works on the GPU box with this command, so the image, the
weights, and the GPU are fine:

    docker run --gpus all --ipc=host -p 8000:8000 \
      -v /home/ubuntu/models/qwen-ft:/models/financial-lora \
      vllm/vllm-openai:v0.10.2 \
      /models/financial-lora --served-model-name financial-lora \
      --port 8000 --max-model-len 4096 --gpu-memory-utilization 0.85

The image comes from upstream and we can't rebuild it. You don't have
access to the cluster from here. Every rollout attempt costs us a GPU node
restart, so please get the manifests right in one go rather than fixing
only the error in the log.

Please fix the manifests so the model server starts and the `api`
deployment can use it again. Explain what was wrong.
