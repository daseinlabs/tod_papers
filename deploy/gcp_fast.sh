#!/usr/bin/env bash
# One A100 40GB spot VM (`tod-fast`) running the TOD picker with the shared-prefix scorer (TOD_SHARED_PREFIX=batched)
# plus deploy/fast_shim.py, which serves the public /v1/systemone request/response shape. Reached only through an SSH
# tunnel (both containers bind 127.0.0.1 on the VM; no public port, no API key). Run from Git Bash in the repo root.
#
#   deploy/gcp_fast.sh start    # create + set up the VM if it does not exist (~15-20 min first time), else resume it
#   deploy/gcp_fast.sh tunnel   # forward localhost:8790 -> VM:8790 (leave running); then TOD_API_URL=http://127.0.0.1:8790
#   deploy/gcp_fast.sh stop     # stop GPU/CPU billing (disk ~$0.03/h remains)
#   deploy/gcp_fast.sh status   # VM status + picker health (via ssh)
#   deploy/gcp_fast.sh setup    # re-ship code and rerun deploy/fast_remote_up.sh on an existing VM
#   deploy/gcp_fast.sh logs     # last picker/shim log lines
#   deploy/gcp_fast.sh down     # delete VM + disk
#   NAME=... ZONE=... LPORT=... MACHINE=... override
#   PROJECT (default: gcloud config), WEIGHTS_BUCKET (gs://... holding the picker bundle, base and golden; required
#   for start/setup), IMAGE (default: us-central1-docker.pkg.dev/$PROJECT/$AR_REPO/tod-picker:3c2eec8)
#
# Same recipe as the shared-prefix benchmark VM (private notes): a2-highgpu-1g, us-central1-c first, Deep Learning VM
# image, deployed image tod-picker:3c2eec8, weights copied read-only from the serving bucket. Code for the overlay
# (parsec-platform / tod_training branch speedup-shared-prefix) is fetched with `gh` at setup time.
set -euo pipefail
D="$(dirname "$0")"
NAME=${NAME:-tod-fast}
ZONE=${ZONE:-$(cat "$D/.zone.fast" 2>/dev/null || echo us-central1-c)}
MACHINE=${MACHINE:-a2-highgpu-1g}
LPORT=${LPORT:-8790}
BRANCH=${BRANCH:-speedup-shared-prefix}
PROJECT=${PROJECT:-$(gcloud config get-value project 2>/dev/null)}
AR_REPO=${AR_REPO:-dasein-images}
IMAGE=${IMAGE:-us-central1-docker.pkg.dev/$PROJECT/$AR_REPO/tod-picker:3c2eec8}
IMAGE_FAMILY=${IMAGE_FAMILY:-common-cu129-ubuntu-2204-nvidia-580}
G="gcloud --project=$PROJECT"
SSH="$G compute ssh $NAME --zone=$ZONE --strict-host-key-checking=no"
cd "$D/.."

setup() {
  : "${WEIGHTS_BUCKET:?set WEIGHTS_BUCKET=gs://<bucket> (picker bundle + base + golden)}"
  local T; T=$(mktemp -d); mkdir -p "$T/fast/overlay" "$T/fast/shim/tod_api_vendor"
  raw() { gh api "repos/daseinlabs/$1/contents/$2?ref=$BRANCH" -H "Accept: application/vnd.github.raw" > "$3"; }
  raw parsec-platform packages/tod-picker/src/tod_picker/server.py "$T/fast/overlay/server.py"
  raw tod_training tod/tod/eval/shared_prefix.py "$T/fast/overlay/shared_prefix.py"
  raw parsec-platform packages/tod-api/src/tod_api/jev.py "$T/fast/shim/tod_api_vendor/jev.py"
  raw parsec-platform packages/tod-api/src/tod_api/images.py "$T/fast/shim/tod_api_vendor/images.py"
  : > "$T/fast/shim/tod_api_vendor/__init__.py"
  cp deploy/fast_shim.py "$T/fast/shim/"; cp deploy/fast_remote_up.sh "$T/fast/"
  local TGZ="$T/fast.tgz"; tar czf "$TGZ" -C "$T" fast
  command -v cygpath >/dev/null && TGZ=$(cygpath -w "$TGZ")
  $G compute scp --zone="$ZONE" --strict-host-key-checking=no "$TGZ" "$NAME":fast.tgz
  $SSH --command="tar xzf fast.tgz && IMG='$IMAGE' BUCKET='${WEIGHTS_BUCKET%/}' bash fast/fast_remote_up.sh"
  rm -rf "$T"
}

case "${1:-}" in
start)
  if st=$($G compute instances describe "$NAME" --zone="$ZONE" --format="value(status)" 2>/dev/null); then
    [ "$st" = RUNNING ] || $G compute instances start "$NAME" --zone="$ZONE"
    echo "$NAME $ZONE: started (containers restart themselves; picker healthy ~3 min after boot)"; exit 0
  fi
  T0=$(date +%s); created=
  for z in $ZONE ${FALLBACK_ZONES:-us-central1-a us-central1-b us-central1-f us-west4-b us-west1-b us-east1-b}; do
    echo "== trying $z"
    if $G compute instances create "$NAME" --zone="$z" --machine-type="$MACHINE" \
        --provisioning-model=SPOT --instance-termination-action=STOP --maintenance-policy=TERMINATE \
        --image-family="$IMAGE_FAMILY" --image-project=deeplearning-platform-release \
        --boot-disk-size=200GB --boot-disk-type=pd-ssd --scopes=cloud-platform \
        --metadata=install-nvidia-driver=True --labels=app=tod-fast; then created=$z; break; fi
  done
  [ -n "$created" ] || { echo "no $MACHINE spot capacity"; exit 1; }
  ZONE=$created; SSH="$G compute ssh $NAME --zone=$ZONE --strict-host-key-checking=no"; echo "$ZONE" > "$D/.zone.fast"
  echo y | $G compute ssh "$NAME" --zone="$ZONE" --command=true >/dev/null 2>&1 || true
  echo "waiting for ssh ..."; for i in $(seq 1 30); do $SSH --command=true && break; sleep 10; done
  echo "waiting for the GPU driver ..."; for i in $(seq 1 40); do $SSH --command='nvidia-smi -L' 2>/dev/null && break; sleep 15; done
  setup
  echo "up in $(( $(date +%s) - T0 ))s ($NAME, $ZONE). now: deploy/gcp_fast.sh tunnel" ;;
setup) setup ;;
tunnel)
  echo y | $G compute ssh "$NAME" --zone="$ZONE" --command=true >/dev/null 2>&1 || true
  echo "tunnel localhost:$LPORT -> $NAME:8790 (Ctrl-C to close)"
  IP=$($G compute instances describe "$NAME" --zone="$ZONE" --format="value(networkInterfaces[0].accessConfigs[0].natIP)")
  KEY="$HOME/.ssh/google_compute_engine"
  if command -v ssh >/dev/null && [ -f "$KEY" ] && [ -n "$IP" ]; then
    ssh -i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ServerAliveInterval=10 \
        -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -N -L $LPORT:localhost:8790 "${SSH_USER:-${USER:-${USERNAME:-$(whoami)}}}@$IP"
  else
    $SSH -- -N -L $LPORT:localhost:8790
  fi ;;
stop)   $G compute instances stop "$NAME" --zone="$ZONE" ;;
down)   $G compute instances delete "$NAME" --zone="$ZONE" --quiet ;;
status)
  $G compute instances describe "$NAME" --zone="$ZONE" --format="value(name,zone.basename(),status,machineType.basename(),scheduling.provisioningModel)"
  $SSH --command='curl -s -m 5 http://127.0.0.1:8790/healthz; echo' 2>/dev/null || true ;;
logs)   $SSH --command='sudo docker logs --tail 20 tod-picker 2>&1; echo ---; sudo docker logs --tail 10 tod-shim 2>&1' ;;
*) sed -n 2,17p "$0"; exit 1 ;;
esac
