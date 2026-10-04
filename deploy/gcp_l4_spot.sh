#!/usr/bin/env bash
# One L4 spot VM on GCP running deploy/Dockerfile, reached through an SSH tunnel
# (no public port is opened). Run from Git Bash in the repo root.
#
#   deploy/gcp_l4_spot.sh up       # create VM, ship code, build image, start server  (~10-15 min first time)
#   deploy/gcp_l4_spot.sh up --on-demand   # same, STANDARD provisioning (no preemption, ~1.5x the spot price)
#   deploy/gcp_l4_spot.sh on-demand # convert the existing VM to STANDARD in place (stop, set-scheduling, start)
#   deploy/gcp_l4_spot.sh spot      # convert it back to SPOT in place
#   deploy/gcp_l4_spot.sh tunnel   # forward localhost:8765 -> VM:8765 (leave running)
#   set TOD_EXTRACT_URL=http://127.0.0.1:8765  then run the loop
#   deploy/gcp_l4_spot.sh stop     # stop billing for GPU/CPU (disk still ~$0.01/h)
#   deploy/gcp_l4_spot.sh start    # resume (container restarts itself)
#   deploy/gcp_l4_spot.sh down     # delete VM + disk
#   deploy/gcp_l4_spot.sh status
#   any command + --a100   # a second, separate box: tod-extract-a100, a2-highgpu-1g (1x A100 40GB, 12 vCPU),
#                          # on-demand, us-west4-b then us-west1-b, us-central1; state in deploy/.zone.a100 (the L4's
#                          # deploy/.name/.zone are not touched); tunnel on localhost:8766
#   MACHINE=... NAME=... STATE=... LPORT=... override any of these
#
# Defaults: us-west4-a (lowest measured RTT from this laptop among L4 regions,
# ~30 ms), g2-standard-8 (OCR is CPU-bound; g2-standard-4 has only 4 vCPUs).
set -euo pipefail
D="$(dirname "$0")"
SCHED="--provisioning-model=SPOT --instance-termination-action=STOP"
case " ${*:2} " in *" --a100 "*)
  NAME=${NAME:-tod-extract-a100}; STATE=${STATE:-.a100}; MACHINE=${MACHINE:-a2-highgpu-1g}; LPORT=${LPORT:-8766}
  SCHED="--provisioning-model=STANDARD"
  FALLBACK_ZONES=${FALLBACK_ZONES:-us-west1-b us-central1-a us-central1-b us-central1-c us-central1-f}
  DEFZONE=us-west4-b ;;   # same region as the L4 (equal RTT), then us-west1-b, us-central1
esac
STATE=${STATE:-}                            # suffix of the state files: deploy/.name$STATE, deploy/.zone$STATE
NAME=${NAME:-$(cat "$D/.name$STATE" 2>/dev/null || echo tod-extract)}   # deploy/.name, written by up
ZONE=${ZONE:-$(cat "$D/.zone$STATE" 2>/dev/null || echo ${DEFZONE:-us-west4-a})}
MACHINE=${MACHINE:-g2-standard-8}          # g2-standard-4 is cheaper; see docs/remote_extraction.md
LPORT=${LPORT:-8765}                       # local end of the tunnel (the server is always VM:8765)
PROJECT=${PROJECT:-$(gcloud config get-value project 2>/dev/null)}
IMAGE_FAMILY=${IMAGE_FAMILY:-common-cu129-ubuntu-2204-nvidia-580}   # Deep Learning VM: driver + docker preinstalled
G="gcloud --project=$PROJECT"
cd "$(dirname "$0")/.."

if [ "${2:-}" = "--on-demand" ]; then
  SCHED="--provisioning-model=STANDARD"
  [ "$NAME" = tod-extract ] && NAME=tod-extract-od   # 2026-10-02: on-demand VM name
fi

case "${1:-}" in
up)
  T0=$(date +%s)
  # spot L4 capacity comes and goes: try $ZONE, then the other L4 zones we have quota in
  ZONES="$ZONE ${FALLBACK_ZONES:-us-west4-c us-west1-a us-west1-b us-west1-c us-west2-b us-west2-c us-central1-a us-central1-b us-central1-c}"
  created=
  for z in $ZONES; do
    echo "== trying $z"
    if $G compute instances create "$NAME" --zone="$z" --machine-type="$MACHINE"       $SCHED --maintenance-policy=TERMINATE       --image-family="$IMAGE_FAMILY" --image-project=deeplearning-platform-release       --boot-disk-size=80GB --boot-disk-type=pd-balanced       --metadata=install-nvidia-driver=True --labels=app=tod-extract; then created=$z; break; fi
  done
  [ -n "$created" ] || { echo "no $MACHINE capacity in: $ZONES"; exit 1; }
  ZONE=$created; echo "$ZONE" > "deploy/.zone$STATE"; echo "$NAME" > "deploy/.name$STATE"; echo "created in $ZONE (+$(( $(date +%s) - T0 ))s)"
  # NB on Windows gcloud drives PuTTY (plink/pscp): no OpenSSH -o flags, Windows paths for scp
  # gcloud drives PuTTY on Windows; a new VM (or a new IP after a preemption) makes it prompt to store the host
  # key, and the tunnel then dies silently. Accept it once non-interactively.
  echo y | $G compute ssh "$NAME" --zone="$ZONE" --command=true >/dev/null 2>&1 || true
  echo "waiting for ssh ..."; for i in $(seq 1 30); do $G compute ssh "$NAME" --zone="$ZONE" --strict-host-key-checking=no --command=true && break; sleep 10; done
  TGZ=$(mktemp -d)/tod_extract_src.tgz
  tar czf "$TGZ" requirements-extract.txt deploy/requirements-server.txt deploy/fetch_models.py deploy/Dockerfile deploy/remote_up.sh src/tod_papers/*.py
  command -v cygpath >/dev/null && TGZ=$(cygpath -w "$TGZ")
  $G compute scp --zone="$ZONE" --strict-host-key-checking=no "$TGZ" "$NAME":tod_extract_src.tgz
  $G compute ssh "$NAME" --zone="$ZONE" --strict-host-key-checking=no     --command='mkdir -p tod && tar xzf tod_extract_src.tgz -C tod && bash tod/deploy/remote_up.sh'
  echo "up in $(( $(date +%s) - T0 ))s (zone $ZONE). now: deploy/gcp_l4_spot.sh tunnel" ;;
tunnel)
  echo y | $G compute ssh "$NAME" --zone="$ZONE" --command=true >/dev/null 2>&1 || true   # store a changed host key
  echo "tunnel localhost:$LPORT -> $NAME:8765 (Ctrl-C to close)"
  IP=$($G compute instances describe "$NAME" --zone="$ZONE" --format="value(networkInterfaces[0].accessConfigs[0].natIP)")
  KEY="$HOME/.ssh/google_compute_engine"
  if command -v ssh >/dev/null && [ -f "$KEY" ] && [ -n "$IP" ]; then
    # plain OpenSSH, no host-key cache: a new IP/host key (recreate, preemption) cannot stall it
    ssh -i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ServerAliveInterval=10         -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -N -L $LPORT:localhost:8765 "${SSH_USER:-${USER:-${USERNAME:-$(whoami)}}}@$IP"
  else
    $G compute ssh "$NAME" --zone="$ZONE" --strict-host-key-checking=no -- -N -L $LPORT:localhost:8765
  fi ;;
stop)   $G compute instances stop "$NAME" --zone="$ZONE" ;;
start)  $G compute instances start "$NAME" --zone="$ZONE" ;;
down)   $G compute instances delete "$NAME" --zone="$ZONE" --quiet ;;
on-demand)   # spot VMs get preempted mid-run (twice in ~1 h on 2026-10-02); the disk + built image are kept
  $G compute instances stop "$NAME" --zone="$ZONE"
  $G compute instances set-scheduling "$NAME" --zone="$ZONE" --no-preemptible --provisioning-model=STANDARD --clear-instance-termination-action --restart-on-failure
  $G compute instances start "$NAME" --zone="$ZONE" ;;
spot)
  $G compute instances stop "$NAME" --zone="$ZONE"
  $G compute instances set-scheduling "$NAME" --zone="$ZONE" --preemptible --provisioning-model=SPOT --instance-termination-action=STOP --no-restart-on-failure
  $G compute instances start "$NAME" --zone="$ZONE" ;;
status) $G compute instances describe "$NAME" --zone="$ZONE" --format="value(status,machineType.basename(),scheduling.provisioningModel)" ;;
*) sed -n 2,23p "$0"; exit 1 ;;
esac
