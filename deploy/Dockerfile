# tod_papers extract_server -- GPU image (CUDA 12.6 runtime, torch cu126, Python 3.13)
# Build from the repo root:   docker build -f deploy/Dockerfile -t tod-extract .
# Run:                         docker run --gpus all -p 8765:8765 tod-extract
FROM nvidia/cuda:12.6.3-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    UV_PYTHON_INSTALL_DIR=/opt/python
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
# Python 3.13 (same as the laptop) via uv's standalone builds
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh && \
    uv venv -p 3.13 /opt/venv
ENV PATH=/opt/venv/bin:$PATH VIRTUAL_ENV=/opt/venv

WORKDIR /app
COPY requirements-extract.txt deploy/requirements-server.txt ./
RUN uv pip install torch==2.14.1 torchvision==0.29.1 --index-url https://download.pytorch.org/whl/cu126 && \
    uv pip install -r requirements-extract.txt -r requirements-server.txt

# weights baked into the image (~1.3 GB): pulled from HF / modelscope at build time
COPY deploy/fetch_models.py deploy/
RUN python deploy/fetch_models.py /app/models

COPY src/ src/
ENV PYTHONPATH=/app/src TOD_OCR_THREADS=8 TOD_EXTRACT_VERBOSE=1
EXPOSE 8765
HEALTHCHECK --interval=30s --start-period=120s CMD curl -fs localhost:8765/health || exit 1
CMD ["python", "-m", "tod_papers.extract_server", "--host", "0.0.0.0", "--port", "8765"]
