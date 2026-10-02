#!/usr/bin/env bash
# Runs on the VM (shipped + invoked by gcp_l4_spot.sh up): build the image, start the server.
set -e
cd ~/tod
until nvidia-smi >/dev/null 2>&1; do echo "waiting for NVIDIA driver install..."; sleep 15; done
nvidia-smi --query-gpu=name,driver_version --format=csv,noheader
# the cu129 DLVM image ships nvidia-container-toolkit but not docker
if ! command -v docker >/dev/null; then
  sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io docker-buildx >/dev/null
  sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
fi
sudo docker build -f deploy/Dockerfile -t tod-extract .
sudo docker rm -f tod-extract 2>/dev/null || true
sudo docker run -d --name tod-extract --gpus all --restart unless-stopped \
  -p 127.0.0.1:8765:8765 -e TOD_OCR_THREADS=$(nproc) tod-extract
until curl -fs localhost:8765/health; do sleep 5; done; echo
