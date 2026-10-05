#!/usr/bin/env bash
# Runs ON the tod-fast VM (shipped by deploy/gcp_fast.sh). Idempotent: re-running reuses the weights and images.
#   1. weights: bundle + pinned base + golden copied read-only from $BUCKET (gs://...) to /data/models
#   2. image tod-fast: the deployed tod-picker:3c2eec8 + the speedup-shared-prefix server.py / shared_prefix.py
#   3. container tod-picker  (127.0.0.1:8080, TOD_SHARED_PREFIX=batched, same env as the Cloud Run picker)
#   4. container tod-shim    (127.0.0.1:8790, deploy/fast_shim.py: /v1/systemone in the public API shape)
# Both containers use --network host and bind 127.0.0.1 only; --restart unless-stopped brings them back after
# `gcp_fast.sh stop` / `start` (the picker reloads the weights and reruns the golden check, ~2-3 min).
set -euo pipefail
cd "$HOME/fast"
IMG=${IMG:?IMG (picker image) is passed by deploy/gcp_fast.sh}
BUCKET=${BUCKET:?BUCKET (gs://... weights) is passed by deploy/gcp_fast.sh}
BASE_REV=base/google/gemma-4-12B-it/707f0a3b8a3c7ad586ed01e27eafbad8a27dd0f7
M=/data/models
sudo mkdir -p $M && sudo chown "$USER" /data $M

if [ ! -f $M/.done ]; then
  echo "== copying weights from $BUCKET (read-only) ..."
  mkdir -p $M/golden "$M/$(dirname $BASE_REV)"
  gcloud storage cp -r $BUCKET/utod-picker-12b-v1 $M/
  gcloud storage cp -r "$BUCKET/$BASE_REV" "$M/$(dirname $BASE_REV)/"
  gcloud storage cp $BUCKET/golden/utod-picker-12b-v1.jsonl $M/golden/
  touch $M/.done
fi
du -sh $M

until nvidia-smi >/dev/null 2>&1; do echo "waiting for NVIDIA driver install..."; sleep 15; done
# the cu129 DLVM image ships nvidia-container-toolkit but not docker (same as deploy/remote_up.sh)
if ! command -v docker >/dev/null; then
  sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io docker-buildx >/dev/null
  sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
fi
# docker runs as root: give root's docker the Artifact Registry credential helper
sudo gcloud auth configure-docker us-central1-docker.pkg.dev --quiet >/dev/null 2>&1
cat > Dockerfile.fast <<EOF
FROM $IMG
COPY overlay/shared_prefix.py /opt/tod/tod/tod/eval/shared_prefix.py
COPY overlay/server.py /tmp/server.py
RUN cp /tmp/server.py "\$(python -c 'import tod_picker.server as s; print(s.__file__)')" && \
    python -c "import tod.eval.shared_prefix, tod_picker.server as s; assert 'batched' in s.SHARED_MODES"
EOF
sudo docker build -q -t tod-fast -f Dockerfile.fast . >/dev/null
echo "== image tod-fast built"

[ -f picker.key ] || (umask 077; head -c 24 /dev/urandom | base64 | tr -d '/+=' > picker.key)
sudo docker rm -f tod-picker tod-shim >/dev/null 2>&1 || true
sudo docker run -d --name tod-picker --gpus all --network host --restart unless-stopped \
  -v $M:/models:ro \
  -e TOD_BUNDLE_DIR=/models/utod-picker-12b-v1 -e TOD_BASE_DIR=/models/$BASE_REV \
  -e TOD_GOLDEN=/models/golden/utod-picker-12b-v1.jsonl -e TOD_REQUIRE_GOLDEN=1 -e TOD_STAGE1=retriever \
  -e TOD_SHARED_PREFIX=batched -e TOD_PICKER_KEY="$(cat picker.key)" \
  tod-fast uvicorn tod_picker.server:create_app --factory --host 127.0.0.1 --port 8080 >/dev/null
sudo docker run -d --name tod-shim --network host --restart unless-stopped \
  -v "$HOME/fast/shim:/shim:ro" -w /shim -e TOD_PICKER_KEY="$(cat picker.key)" \
  tod-fast uvicorn fast_shim:app --host 127.0.0.1 --port 8790 >/dev/null
echo "== containers started; waiting for the picker to load (weights + golden check) ..."
for i in $(seq 1 90); do
  if curl -sf http://127.0.0.1:8790/healthz >/dev/null; then curl -s http://127.0.0.1:8790/healthz; echo; exit 0; fi
  sleep 10
done
echo "picker not healthy after 15 min"; sudo docker logs --tail 30 tod-picker; exit 1
