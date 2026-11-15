# Same build as the laptop (R17). CUDA base so the worker can run
# faster-whisper on the GPU; the API container works without a GPU.
# Untested in the build environment (no Docker or GPU there): verify in M3.
FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 SCRIBE_VAULT=/vault SCRIBE_BIND=0.0.0.0
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv python3-pip ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY scribe ./scribe
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir ".[stt]"
ENV PATH=/opt/venv/bin:$PATH
# Speech models live in the vault volume so they survive rebuilds and are
# fetched once, online, by `scribe setup`.
ENV HF_HOME=/vault/models
VOLUME /vault
EXPOSE 8765
CMD ["scribe", "serve"]
