#!/usr/bin/env bash
# One L4 spot VM on GCP running deploy/Dockerfile, reached through an SSH tunnel
# (no public port is opened). Run from Git Bash in the repo root.
#
#   deploy/gcp_l4_spot.sh up       # create VM, ship code, build image, start server  (~10-15 min first time)
#   deploy/gcp_l4_spot.sh tunnel   # forward localhost:8765 -> VM:8765 (leave running)
#   set TOD_EXTRACT_URL=http://127.0.0.1:8765  then run the loop
#   deploy/gcp_l4_spot.sh stop     # stop billing for GPU/CPU (disk still ~$0.01/h)
#   deploy/gcp_l4_spot.sh start    # resume (container restarts itself)
#   deploy/gcp_l4_spot.sh down     # delete VM + disk
#   deploy/gcp_l4_spot.sh status
#
# Defaults: us-west4-a (lowest measured RTT from this laptop among L4 regions,
# ~30 ms), g2-standard-8 (OCR is CPU-bound; g2-standard-4 has only 4 vCPUs).
set -euo pipefail
NAME=${NAME:-tod-extract}
ZONE=${ZONE:-$(cat "$(dirname "$0")/.zone" 2>/dev/null || echo us-west4-a)}
MACHINE=${MACHINE:-g2-standard-8}          # g2-standard-4 is cheaper; see docs/remote_extraction.md
PROJECT=${PROJECT:-$(gcloud config get-value project 2>/dev/null)}
IMAGE_FAMILY=${IMAGE_FAMILY:-common-cu129-ubuntu-2204-nvidia-580}   # Deep Learning VM: driver + docker preinstalled
G="gcloud --project=$PROJECT"
cd "$(dirname "$0")/.."

case "${1:-}" in
up)
  T0=$(date +%s)
  # spot L4 capacity comes and goes: try $ZONE, then the other L4 zones we have quota in
  ZONES="$ZONE ${FALLBACK_ZONES:-us-west4-c us-west1-a us-west1-b us-west1-c us-west2-b us-west2-c us-central1-a us-central1-b us-central1-c}"
  created=
  for z in $ZONES; do
    echo "== trying $z"
    if $G compute instances create "$NAME" --zone="$z" --machine-type="$MACHINE"       --provisioning-model=SPOT --instance-termination-action=STOP --maintenance-policy=TERMINATE       --image-family="$IMAGE_FAMILY" --image-project=deeplearning-platform-release       --boot-disk-size=80GB --boot-disk-type=pd-balanced       --metadata=install-nvidia-driver=True --labels=app=tod-extract; then created=$z; break; fi
  done
  [ -n "$created" ] || { echo "no spot L4 capacity in: $ZONES"; exit 1; }
  ZONE=$created; echo "$ZONE" > deploy/.zone; echo "created in $ZONE (+$(( $(date +%s) - T0 ))s)"
  # NB on Windows gcloud drives PuTTY (plink/pscp): no OpenSSH -o flags, Windows paths for scp
  echo "waiting for ssh ..."; for i in $(seq 1 30); do $G compute ssh "$NAME" --zone="$ZONE" --strict-host-key-checking=no --command=true && break; sleep 10; done
  TGZ=$(mktemp -d)/tod_extract_src.tgz
  tar czf "$TGZ" requirements-extract.txt deploy/requirements-server.txt deploy/fetch_models.py deploy/Dockerfile deploy/remote_up.sh src/tod_papers/*.py
  command -v cygpath >/dev/null && TGZ=$(cygpath -w "$TGZ")
  $G compute scp --zone="$ZONE" --strict-host-key-checking=no "$TGZ" "$NAME":tod_extract_src.tgz
  $G compute ssh "$NAME" --zone="$ZONE" --strict-host-key-checking=no     --command='mkdir -p tod && tar xzf tod_extract_src.tgz -C tod && bash tod/deploy/remote_up.sh'
  echo "up in $(( $(date +%s) - T0 ))s (zone $ZONE). now: deploy/gcp_l4_spot.sh tunnel" ;;
tunnel)
  echo "tunnel localhost:8765 -> $NAME:8765 (Ctrl-C to close)"
  $G compute ssh "$NAME" --zone="$ZONE" --strict-host-key-checking=no -- -N -L 8765:localhost:8765 ;;
stop)   $G compute instances stop "$NAME" --zone="$ZONE" ;;
start)  $G compute instances start "$NAME" --zone="$ZONE" ;;
down)   $G compute instances delete "$NAME" --zone="$ZONE" --quiet ;;
status) $G compute instances describe "$NAME" --zone="$ZONE" --format="value(status,machineType.basename(),scheduling.provisioningModel)" ;;
*) sed -n 2,16p "$0"; exit 1 ;;
esac
