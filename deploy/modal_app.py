"""Alternative: Modal serverless L4 (workspace 'dasein' is already logged in on this laptop).

    modal deploy deploy/modal_app.py      # prints https://<workspace>--tod-extract-web.modal.run
    set TOD_EXTRACT_URL=<that url>

Billed per second while a container is up ($0.80/h L4 + CPU/RAM; $30/month
free credit on Starter). Scales to zero after SCALEDOWN s idle; a cold start
(image pull + model load + warmup) is ~30-60 s, so open the session with a
/health call. Uses the same deploy/Dockerfile. Not yet deployed or tested.
"""
import pathlib

import modal

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCALEDOWN = 600

image = modal.Image.from_dockerfile(str(ROOT / "deploy" / "Dockerfile"), context_dir=str(ROOT))
app = modal.App("tod-extract", image=image)


@app.function(gpu="L4", cpu=8, memory=16384, scaledown_window=SCALEDOWN, max_containers=1, timeout=120)
@modal.concurrent(max_inputs=1)
@modal.asgi_app()
def web():
    from tod_papers.extract_server import app as fastapi_app  # lifespan hook warms the models

    return fastapi_app
