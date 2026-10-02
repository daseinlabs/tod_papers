r"""Convert OmniParser-v2 icon_caption (Florence-2-base-ft, remote-code layout) to the
native transformers>=5 Florence2ForConditionalGeneration layout.

  .venv-loop\Scripts\python scripts\convert_omniparser_caption.py
reads  models/omniparser-v2/icon_caption/model.safetensors
       models/florence2-base/  (config/processor/tokenizer, native layout)
writes models/omniparser-caption-hf/  (config + processor from florence2-base, converted weights)
"""
import os, re, shutil, sys
from safetensors import safe_open
from safetensors.torch import save_file

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "models", "omniparser-v2", "icon_caption", "model.safetensors")
REF = os.path.join(ROOT, "models", "florence2-base")
DST = os.path.join(ROOT, "models", "omniparser-caption-hf")

RULES = [
    (r"^language_model\.model\.", "model.language_model."),
    (r"^language_model\.lm_head\.", "lm_head."),
    (r"^image_pos_embed\.", "model.multi_modal_projector.image_position_embed."),
    (r"^image_proj_norm\.", "model.multi_modal_projector.image_proj_norm."),
    (r"^image_projection$", "model.multi_modal_projector.image_projection.weight"),
    (r"^visual_temporal_embed\.", "model.multi_modal_projector.visual_temporal_embed."),
    (r"^vision_tower\.", "model.vision_tower."),
    (r"channel_attn\.norm\.", "norm1."),
    (r"window_attn\.norm\.", "norm1."),
    (r"ffn\.norm\.", "norm2."),
    (r"conv1\.fn\.dw\.", "conv1."),
    (r"conv2\.fn\.dw\.", "conv2."),
    (r"ffn\.fn\.net\.", "ffn."),
    (r"(convs\.[0-9]+)\.proj\.", lambda m: m.group(1) + ".conv."),
    (r"\.fn\.", "."),
]
SKIP = {"language_model.final_logits_bias", "language_model.lm_head.weight"}  # tied to shared


def conv(k):
    for a, b in RULES:
        k = re.sub(a, b, k)
    return k


def main(check_only=False):
    with safe_open(os.path.join(REF, "model.safetensors"), "pt") as f:
        ref = {k: tuple(f.get_slice(k).get_shape()) for k in f.keys()}
    out, miss = {}, []
    with safe_open(SRC, "pt") as f:
        for k in f.keys():
            t = f.get_tensor(k)
            if k in SKIP:
                continue
            nk = conv(k)
            if nk.endswith("shared.weight") and t.shape[0] < ref[nk][0]:
                # florence-community tokenizer has 39 extra (unused) rows; pad
                import torch
                t = torch.cat([t, t.new_zeros(ref[nk][0] - t.shape[0], t.shape[1])])
            if nk not in ref:
                miss.append((k, nk, tuple(t.shape)))
                continue
            if tuple(t.shape) != ref[nk]:
                if t.ndim == 2 and tuple(t.T.shape) == ref[nk]:
                    t = t.T.contiguous()
                else:
                    miss.append((k, nk, tuple(t.shape), ref[nk]))
                    continue
            out[nk] = t.half()
    unfilled = [k for k in ref if k not in out]
    print("unmapped src:", miss[:40], len(miss))
    print("unfilled dst:", unfilled[:40], len(unfilled))
    if check_only:
        return
    os.makedirs(DST, exist_ok=True)
    for fn in os.listdir(REF):
        if fn != "model.safetensors" and not fn.startswith("."):
            p = os.path.join(REF, fn)
            if os.path.isfile(p):
                shutil.copy(p, DST)
    save_file(out, os.path.join(DST, "model.safetensors"), metadata={"format": "pt"})
    print("wrote", DST)


if __name__ == "__main__":
    main("--check" in sys.argv)
