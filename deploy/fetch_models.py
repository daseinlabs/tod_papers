"""Download the weights extract.py loads into <repo>/models (used by the Dockerfile).

Same files as docs/extraction.md "Model files": OmniParser v2 icon_detect,
RapidOCR 3.9.2 ONNX (PP-OCRv4 det, PP-OCRv5 rec, PP-OCRv4 en rec), Grounding
DINO tiny, CLIP ViT-B/16 (re-saved as safetensors like the local copy).
"""
import os
import shutil
import sys
import urllib.request

from huggingface_hub import hf_hub_download, snapshot_download

M = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "models")
os.makedirs(os.path.join(M, "rapidocr"), exist_ok=True)

p = hf_hub_download("microsoft/OmniParser-v2.0", "icon_detect/model.pt")
shutil.copy(p, os.path.join(M, "icon_detect_model.pt"))

RO = "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/onnx"
for rel, name in [("PP-OCRv4/det", "ch_PP-OCRv4_det_mobile.onnx"),
                  ("PP-OCRv5/rec", "ch_PP-OCRv5_rec_mobile.onnx"),
                  ("PP-OCRv4/rec", "en_PP-OCRv4_rec_mobile.onnx")]:
    urllib.request.urlretrieve(f"{RO}/{rel}/{name}", os.path.join(M, "rapidocr", name))

snapshot_download("IDEA-Research/grounding-dino-tiny", local_dir=os.path.join(M, "grounding-dino-tiny"))

clip_dir = os.path.join(M, "clip-vit-base-patch16")
src = snapshot_download("openai/clip-vit-base-patch16",
                        allow_patterns=["*.json", "*.txt", "pytorch_model.bin", "*.safetensors"])
from transformers import CLIPModel, CLIPProcessor  # noqa: E402

CLIPModel.from_pretrained(src).save_pretrained(clip_dir)
CLIPProcessor.from_pretrained(src).save_pretrained(clip_dir)
for d, _, fs in os.walk(M):
    for f in fs:
        print(f"{os.path.getsize(os.path.join(d, f)) / 1e6:8.1f} MB  {os.path.relpath(os.path.join(d, f), M)}")
