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

No cloud resources were created.

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

## Cost

| Option | $/h while on | Notes |
|---|---|---|
| **GCP g2-standard-8 spot, us-west4** | **~$0.57** (us-west1/central1: $0.51) | Default in `gcp_l4_spot.sh`. Preemptible: the VM stops and `start` brings it back. |
| GCP g2-standard-4 spot | ~$0.48 (us-west1/central1: $0.42) | Half the vCPUs, so slower OCR. |
| GCP g2-standard-4 on-demand | $0.71–0.80 | No preemption. |
| Boot disk 80 GB pd-balanced | ~$0.01/h, $8/month | Billed even when the VM is stopped. Use `down` to delete it. |
| Modal L4 serverless | $0.80 GPU + ~$0.38 (8 cores) + ~$0.13 (16 GiB) ≈ **$1.30/h** while a container is up | Scales to zero after 10 min idle. $30/month free credit is ~20 h. Cold start ~30–60 s. |
| RunPod / Vast / Lambda | — | No account or CLI on this machine. |

A 2-hour play session on GCP g2-standard-8 spot costs about **$1.20**. Remember `stop`.

## Go / no-go

**GO, with one caveat to check on the first real run.** Remote extraction should win
whenever the game is running:

1. **Transport is cheap.** The native-PNG trick makes the frame 22–24 KB. The total
   transport tax is ~40 ms of wire time plus the 33 ms RTT, under 100 ms in all.
2. **Latency.** The laptop under game load measured 2.2–5.9 s per frame. An L4 box that
   is not running the game should give ~0.85–1.35 s end to end. That is a **2–4× win**.
   The game, the loop and the TOD requests also get the laptop's GPU and CPU back.
3. **Correctness.** The output is bit-identical to local extract() (verified on 2
   frames), so prompts, Set-of-Mark and stuck detection behave exactly the same.

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

`.venv-loop` already has fastapi, uvicorn and python-multipart (pins in
`deploy/requirements-server.txt`). The client only needs `requests`, which is already
installed.

### loop.py change (not applied; for loop.py's owner)

loop.py imports `from . import extract as ex` and calls `ex.warmup()` and
`ex.extract(frame)`. It also uses `ex.describe`, `ex.COUNTER_CAP` and `ex.LAST_TIMINGS`,
and those must keep pointing at the real module. Swap only the two calls:

```python
# near the imports
_EXTRACT_URL = os.environ.get("TOD_EXTRACT_URL")
if _EXTRACT_URL: from . import extract_remote as _exr
_extract = _exr.extract if _EXTRACT_URL else ex.extract;  _warmup = _exr.warmup if _EXTRACT_URL else ex.warmup
```

Then replace `ex.extract(frame)` with `_extract(frame)` (lines ~763 and ~883), and
`ex.warmup()` with `_warmup()` (lines ~754 and ~830). Nothing else changes:
`extract_remote` writes `ex.LAST_TIMINGS` and `ex._LAST_WH`, so
`rec["extract_timings"]` and `ex.describe(b, W, H)` keep working. In remote mode
`extract.py` never loads any model, so nothing extra ends up on the laptop's GPU.

### Modal instead of GCP

```
modal deploy deploy/modal_app.py     # builds the same Dockerfile on Modal
set TOD_EXTRACT_URL=https://WORKSPACE--tod-extract-web.modal.run
```

Modal is public HTTPS, so set `TOD_EXTRACT_TOKEN` as a Modal secret and in the local env.
It is more expensive per hour, but it scales to zero and needs no stop/start.

## Not done / untested

- Neither the Docker image nor the deploy scripts have been built or run (Docker is not
  installed locally, and no cloud resources were created for this task). Expect possible
  first-build fixes. The likely ones are the `uv` Python 3.13 install and `libgl1` for
  opencv.
- The L4 numbers are estimates from the 4070 measurements.
