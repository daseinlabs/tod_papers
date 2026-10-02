# Remote extraction (cloud L4): design, measurements, go/no-go

Problem: `extract()` (YOLO icon_detect + Grounding DINO + PP-OCRv5 + CLIP) takes 1–4 s
per frame on the laptop while the game is running. It also competes with the game for
the RTX 4070 (8 GB) and the i9-13900H. The fix is to run the same pipeline on a GPU box
and send it only the frame.

## Pieces

| File | What |
|---|---|
| `src/tod_papers/extract_server.py` | FastAPI app. `POST /extract` takes PNG/JPEG/WebP as multipart (`image` field), JSON `{"image_b64": ...}` or a raw body, plus optional `scale`. It runs `extract.extract()` unchanged and returns `{W, H, boxes:[Box fields + "describe"], timings}`. `timings` = `extract.LAST_TIMINGS` plus `decode_ms`, `body_ms`, `lock_wait_ms` and `server_ms`. Models are warmed at startup, and `GET /health` reports `warm`, the GPU and the OCR engine. Calls are serialised because extract() has module state. If `TOD_EXTRACT_TOKEN` is set, a bearer token is required. |
| `src/tod_papers/extract_remote.py` | `extract_remote(frame_bgr, url) -> list[Box]` and `extract(frame_bgr)`, with the same signature as `extract.extract` (URL comes from `TOD_EXTRACT_URL`). It also fills `extract.LAST_TIMINGS` (server stages prefixed `srv_`, plus `encode_ms`, `rtt_ms`, `net_ms`, `total_ms`, `bytes`) and `extract._LAST_WH`, so `ex.describe(b)` and loop.py's logs keep working. |
| `tools/check_remote_extract.py` | Equality and latency check: remote against local on saved frames. |
| `deploy/Dockerfile` | CUDA 12.6 cuDNN runtime, Python 3.13 (uv), torch 2.14.1+cu126, the pinned `requirements-extract.txt` and `deploy/requirements-server.txt`. Weights (~1.3 GB) are baked in at build time by `deploy/fetch_models.py` from HF and modelscope. |
| `deploy/docker-compose.yml` | For any Linux box with an NVIDIA GPU and nvidia-container-toolkit. Binds to loopback; reach it over an SSH tunnel. |
| `deploy/remote_up.sh` | Runs on the VM: waits for the NVIDIA driver, installs docker.io and the nvidia runtime (the cu129 DLVM image ships nvidia-container-toolkit but **not** docker), builds the image, starts the container on 127.0.0.1:8765 and waits for `/health`. Re-run it by hand after a preemption mid-build. |
| `deploy/gcp_l4_spot.sh` | `up / tunnel / stop / start / down / status` for one GCP L4 **spot** VM. The VM uses a Deep Learning VM image (driver and docker preinstalled), builds the image on the VM and is reached through an SSH tunnel (no public port). |
| `deploy/modal_app.py` | Alternative: Modal serverless L4 built from the same Dockerfile. |

### Wire format: send the native frame, not a JPEG

Papers, Please draws 570×320 art at exactly 4×. On both test frames every 4×4 cell is a
single colour, so `frame[::4, ::4]` loses nothing. With `TOD_EXTRACT_FMT=auto` (the
default), the client checks this in about 8 ms. It then sends the **570×320 frame as
lossless PNG (~22–24 KB)** with `scale=4`, and the server rebuilds the frame with
`cv2.resize(INTER_NEAREST)`. The rebuilt frame is bit-identical to the original, so
the boxes are **identical** to a local `extract()`. If a frame is not exactly
replicated, the client falls back to JPEG.

JPEG q90 is 10× bigger (~250 KB). It is also **not equivalent**: on the test frames it
gave 69 instead of 67 boxes and 53 instead of 56. It changed CLIP labels, dropped
"APPROVED"/"DENIED" icon text on one frame and added a spurious "rubber stamp" object.
Use `TOD_EXTRACT_FMT=jpeg` only for non-pixel-art sources.

## Cloud access found on this laptop (2026-10-02)

| Provider | State |
|---|---|
| **GCP** (`gcloud`) | Authenticated. Active account is the service account `REDACTED…`; user accounts `REDACTED`, `REDACTED` and `REDACTED` are also credentialed. Project `REDACTED`, default zone us-central1-a. **L4 quota is 16 on-demand and 16 spot** in us-central1, us-west1, us-west2 and us-west4 (1 L4 already in use in us-central1). L4 is available in us-west4-a/c, us-west1-a/b/c and us-central1-a/b/c. The Deep Learning VM family `common-cu129-ubuntu-2204-nvidia-580` exists. |
| **Modal** (`modal` 1.4.1) | Logged in; profile and workspace `dasein`. |
| AWS, Azure, RunPod, Vast, Lambda | No CLI and no credential env vars. |
| Docker | Not installed locally (there is a `~/.docker` dir). WSL is present. |

At the time of the design pass no cloud resources existed. The L4 VM `tod-extract` was
created on 2026-10-02 (see "L4 measured" below).

## Measurements

**Network (this laptop, 2026-10-02):**

- Upload to speed.cloudflare.com: 31 Mbit/s for 250 KB, 46 for 1 MB, 34 for 5 MB.
- Warm HTTPS RTT, gcping (Cloud Run front ends, so this includes TLS/HTTP overhead):

| Region | Warm RTT |
|---|---|
| us-west2 | 23 ms (no L4 zone listed) |
| **us-west4** | **33 ms** |
| us-central1 | 54 ms |
| us-west1 | 70 ms |
| us-east* | 80–87 ms |

| Payload | Size | Upload at ~35 Mbit/s | Plus RTT (us-west4) | Per tick |
|---|---|---|---|---|
| native PNG (default) | 22–24 KB | ~6 ms | ~33 ms | **~40 ms** |
| full-frame JPEG q90 | ~250 KB | ~57 ms | ~33 ms | ~90 ms |
| full-frame PNG | ~280 KB | ~65 ms | ~33 ms | ~100 ms |

Egress cost is negligible. Uploads to GCP are free. The response is ~10–20 KB of
JSON, about 15 MB/h at 1 tick/s, or <$0.01/h of internet egress.

**Local round-trip** (server on this laptop, RTX 4070, game running; the test client
also had its own model copy loaded):

| Frame | local `extract()` | `extract_remote` (auto) | of which encode / wire+HTTP | identical boxes |
|---|---|---|---|---|
| `20261002_003519/raw_0014` | 1.1 s / 4.7–5.4 s | 1.0 s / 5.0–5.9 s | 33–42 ms / 4–7 ms | **yes (67/67)**, `describe()` identical |
| `20261001_221705/raw_0030` | 0.86 s / 3.8–3.9 s | 1.0 s / 3.7–4.3 s | 35–37 ms / 4–7 ms | **yes (56/56)**, `describe()` identical |

The first number in each pair is from the first run. The second is from later runs,
after the machine got busier (CPU at 55–60%). Transport overhead (client encode, wire,
decode, JSON) totals **~45 ms**. Everything else is the pipeline.

Server stage breakdown under contention (`raw_0030`): icon 130–290, gdino 1750–2250,
clip 350–450, ocr (CPU thread) 3800–4200, merge 90–110 ms. **OCR is the critical
path.** It is CPU-bound, so it gets slower when the game and the loop run beside it, and
the GPU cannot fix that.

## L4 latency estimate

Inputs are the quiet-machine 4070 numbers (docs/extraction.md): icon 40–48, gdino
250–370, clip 170–230, OCR 410–810 ms, all on a quiet i9-13900H. An L4 is roughly a
4070-class card for these small fp16 models, so assume about the same GPU time
(~0.5–0.65 s serial, overlapped with OCR). OCR runs on the VM's vCPUs. On G2 these are
Cascade Lake at 2.2/3.7 GHz, slower per core than the laptop's P-cores, and 4 vCPU is
only 2 physical cores.

| Box | GPU stages | OCR (CPU) | merge | **server** | **+ net (us-west4)** |
|---|---|---|---|---|---|
| g2-standard-8 (8 vCPU) | 0.5–0.65 s | 0.7–1.2 s | ~0.1 s | **0.8–1.3 s** | **~0.85–1.35 s** |
| g2-standard-4 (4 vCPU) | 0.5–0.65 s | 1.1–2.0 s | ~0.1 s | 1.2–2.1 s | ~1.25–2.15 s |
| laptop 4070, quiet | | | | 0.5–0.8 s | |
| laptop 4070, game + loop running (measured) | | | | **2.2–5.9 s** | |

These are estimates, not measured on an L4. After `up`, run
`tools/check_remote_extract.py http://127.0.0.1:8765 <frames>` through the tunnel to
get real numbers. It prints the server stage times and the network share separately.

## L4 measured (2026-10-02, g2-standard-8 spot, us-west4-a)

`up` to a warm `/health`: **15.5 min** wall time. That breaks down as VM create 20 s, sshd
plus the driver install ~2 min, docker install ~1 min, image build ~7.5 min (pip ~1 min,
weights ~45 s, layer export/unpack ~5 min), model warmup 12 s. It includes one **spot
preemption 6 min in** (mid-build). After `start` the build was re-run by hand
(`nohup bash tod/deploy/remote_up.sh`). Expect ~10 min without a preemption.
Restarting a stopped VM is fast: the image and container persist, and the container
uses `--restart unless-stopped`.

Client: this laptop, over the `gcp_l4_spot.sh tunnel` SSH forward (PuTTY), with the
game and the live loop running. There were 3 calls per frame; the median is reported.
Server stages are in ms, and the GPU stages run alongside OCR.

| Frame | boxes local / L4 | **L4 round-trip median** (3 runs) | srv total | ocr | gdino | clip | icon | merge | encode | net+HTTP | local `extract()` same session |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `20261002_003519/raw_0000` | 56 / 56 | **1.23 s** (1.19, 1.27, 1.23) | 1052 | 990 | 375 | 172 | 24 | 60 | 55 | 143 | 9.7 s |
| `20261002_003519/raw_0014` | 67 / 67 | **1.26 s** (1.16, 1.26, 4.48) | 1038 | 988 | 350 | 194 | 24 | 48 | 50 | 187 | 5.2 s |
| `20261002_003519/raw_0020` | 61 / 60 | **1.08 s** (5.93, 1.08, 1.08) | 967 | 924 | 431 | 203 | 24 | 41 | 48 | 51 | 5.4 s |
| `20261002_003519/raw_0033` | 56 / 56 | **0.74 s** (0.83, 0.73, 0.74) | 617 | 602 | 354 | 185 | 31 | 13 | 48 | 93 | 3.5 s |
| `20261001_221705/raw_0030` | 56 / 56 | **0.94 s** (0.94, 0.89, 0.96) | 786 | 744 | 371 | 172 | 23 | 41 | 60 | 42 | 4.2 s |
| scratchpad `loop5/live_20261002` | 62 / 62 | **0.85 s** (0.80, 0.85, 0.87) | 727 | 698 | 356 | 168 | 24 | 16 | 60 | 44 | 3.8 s |

- **All six medians are under 1.3 s (0.74-1.26 s)**, against 3.5-9.7 s for local
  `extract()` in the same session. That is a 4-8x win, and better than the 2-4x
  estimate. The tuning step (OCR threads, DINO half precision, batched CLIP) was
  therefore not needed. Notes for later: DINO and CLIP already run fp16 and CLIP already
  batches its crops. OCR is the critical path (600-990 ms on 8 Cascade Lake vCPUs,
  `TOD_OCR_THREADS=8`), and the GPU stages finish inside the OCR window. The next lever
  is g2-standard-12/16 (more OCR cores), not GPU work.
- Two of the 18 calls were 4.5 s and 5.9 s tail outliers. The tunnel or a first-shape
  OCR warmup are suspects; this was not investigated. Keep `TOD_EXTRACT_TIMEOUT` at
  >=10 s.
- **The boxes are *not* bit-identical across GPUs** (the laptop-hosted server *was*
  identical to local). OCR text is identical everywhere, because OCR runs on the CPU.
  fp16 YOLO/GDINO/CLIP numerics on the L4 move some icon and panel edges by 1 px. Of
  358 boxes, 353 matched kind, text and caption within 4 px. The rest: raw_0000 had one
  icon caption "possibly booth" vs "possibly desk"; raw_0020 lost an "arrow" icon and
  gained a "possibly map" panel (61->60); raw_0030 gained a "possibly dark empty
  background" panel. Every repeat on the L4 gave identical boxes, so the L4 is
  deterministic. Stuck detection will see one shift when switching between local and
  remote, but not from tick to tick.

## Cost

| Option | $/h while on | Notes |
|---|---|---|
| **GCP g2-standard-8 spot, us-west4** | **~$0.57** (us-west1/central1: $0.51) | Default in `gcp_l4_spot.sh`. **Measured:** 0.74-1.26 s median round-trip, 15.5 min to ready the first time. Preemptible: it was preempted once 6 min after creation. The VM stops, and `start` brings it back. |
| GCP g2-standard-4 spot | ~$0.48 (us-west1/central1: $0.42) | Half the vCPUs, so slower OCR. |
| GCP g2-standard-4 on-demand | $0.71–0.80 | No preemption. |
| Boot disk 80 GB pd-balanced | ~$0.01/h, $8/month | Billed even when the VM is stopped. Use `down` to delete it. |
| Modal L4 serverless | $0.80 GPU + ~$0.38 (8 cores) + ~$0.13 (16 GiB) ≈ **$1.30/h** while a container is up | Scales to zero after 10 min idle. $30/month free credit is ~20 h. Cold start ~30–60 s. |
| RunPod / Vast / Lambda | — | No account or CLI on this machine. |

A 2-hour play session on GCP g2-standard-8 spot costs about **$1.20**, and the first `up` costs about $0.15 (15 min). That works out to about **$0.0002 per extracted frame** at ~1 frame/s. Remember `stop`.

## Go / no-go

**GO, with one caveat to check on the first real run.** Remote extraction should win
whenever the game is running:

1. **Transport is cheap.** The native-PNG trick makes the frame 22–24 KB. The total
   transport tax is ~40 ms of wire time plus the 33 ms RTT, under 100 ms in all.
2. **Latency.** The laptop under game load measured 2.2–5.9 s per frame. An L4 box that
   is not running the game should give ~0.85–1.35 s end to end. That is a **2–4× win**.
   The game, the loop and the TOD requests also get the laptop's GPU and CPU back.
3. **Correctness.** Transport is lossless: the laptop-hosted server was bit-identical
   to local. On the L4, OCR text is identical, but fp16 GPU numerics shift a few edges by
   1 px and flip ~1 icon or panel caption per frame (see "L4 measured"). The L4 itself
   is deterministic.

The caveat: OCR is CPU-bound, and a g2's Cascade Lake vCPUs are slower per core than
the laptop's i9. That is why the default is **g2-standard-8, not g2-standard-4**. If
`check_remote_extract.py` shows `ocr_ms` > 1.2 s on the VM, try `TOD_OCR_THREADS` =
vCPU count (already set), then g2-standard-12.

On a **quiet** laptop (game paused), local 0.5–0.8 s still beats remote. Remote extraction
only pays off while the game is loading the machine, which is always the case during a
live loop.

## Turning it on

One-time:

```
# laptop, Git Bash, repo root (uses the active gcloud account/project)
deploy/gcp_l4_spot.sh up          # ~10-15 min first time: VM + driver + docker build (~1.3 GB weights)
```

Each session:

```
deploy/gcp_l4_spot.sh start       # if stopped
deploy/gcp_l4_spot.sh tunnel      # leave running in its own terminal
set TOD_EXTRACT_URL=http://127.0.0.1:8765
.venv-loop\Scripts\python tools\check_remote_extract.py %TOD_EXTRACT_URL% runs\20261001_221705\raw_0030.png
.venv-loop\Scripts\python -m tod_papers.loop ...
deploy/gcp_l4_spot.sh stop        # when done (stops GPU billing)
```

**Current state (2026-10-02): VM `tod-extract` is RUNNING in us-west4-a** (recorded in
`deploy/.zone`, which `gcp_l4_spot.sh` reads). To stop it from any shell:

```
gcloud compute instances stop tod-extract --zone=us-west4-a --project=REDACTED
```

After a preemption plus `start`, the VM gets a new IP and host key, and PuTTY's
"Store key in cache?" prompt silently stalls `tunnel`. Run
`echo y | gcloud compute ssh tod-extract --zone=us-west4-a --command=true` once, then
`tunnel` again. The container restarts on its own, warm in ~23 s.

Use `deploy/gcp_l4_spot.sh down` to delete it together with its disk, which stops the
~$8/month disk charge.

`.venv-loop` already has fastapi, uvicorn and python-multipart (pins in
`deploy/requirements-server.txt`). The client only needs `requests`, which is already
installed.

### loop.py change (not applied; for loop.py's owner)

This is three added lines right after `from . import extract as ex` (line 45). It rebinds
the two entry points on the module, so the `ex.warmup()` and `ex.extract(frame)` call
sites (currently 950/958 and 1032/1085) do not change, and the diff survives line
shifts. `ex.describe`, `ex.COUNTER_CAP`, `ex.LAST_TIMINGS` and `ex._LAST_WH` stay the
real module's. `extract_remote` fills `LAST_TIMINGS`/`_LAST_WH` and never calls
`ex.extract`, so there is no recursion. In remote mode no model is ever loaded on the
laptop.

```diff
--- a/src/tod_papers/loop.py
+++ b/src/tod_papers/loop.py
@@ -45,2 +45,5 @@
 from . import extract as ex
+if os.environ.get("TOD_EXTRACT_URL"):  # remote extraction (docs/remote_extraction.md)
+    from . import extract_remote as _exr
+    ex.extract, ex.warmup = _exr.extract, _exr.warmup
 from .extract import Box
```

The loop is unchanged when `TOD_EXTRACT_URL` is unset.

### Modal instead of GCP

```
modal deploy deploy/modal_app.py     # builds the same Dockerfile on Modal
set TOD_EXTRACT_URL=https://WORKSPACE--tod-extract-web.modal.run
```

Modal is public HTTPS, so set `TOD_EXTRACT_TOKEN` as a Modal secret and in the local env.
It is more expensive per hour, but it scales to zero and needs no stop/start.

## Not done / untested

- Built and run on GCP 2026-10-02. There were three fixes to `gcp_l4_spot.sh`/`remote_up.sh`:
  docker is missing on the DLVM image, Windows gcloud drives PuTTY (no OpenSSH `-o`
  flags, Windows paths for scp), and there is zone fallback for spot capacity.
- `deploy/modal_app.py` and `docker-compose.yml` have still not been run.
- The cause of the 2/18 tail outliers (4.5-5.9 s) is unknown.
