from __future__ import annotations

import io
import os

from dotenv import dotenv_values

from app.config import ROOT


class ClipDependencyError(RuntimeError):
    pass


class OpenClipImageEmbedder:
    MODEL_NAME = "ViT-B-32"
    PRETRAINED_NAME = "laion2b_s34b_b79k"
    DIMENSION = 512

    def __init__(self):
        try:
            import torch # type: ignore
            import open_clip # type: ignore
            from PIL import Image # type: ignore
        except ImportError as exc:
            raise ClipDependencyError(
                "Thiếu thư viện CLIP. Cài bằng: "
                "pip install open_clip_torch pillow"
            ) from exc

        self.torch = torch
        self.open_clip = open_clip
        self.Image = Image

        values = {**dotenv_values(ROOT / ".env"), **os.environ}
        requested = str(values.get("PRODUCT_CLIP_DEVICE") or "auto").strip().lower()

        if requested == "auto":
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
        else:
            self.device = requested

        self.model, _, self.preprocess = open_clip.create_model_and_transforms(
            self.MODEL_NAME,
            pretrained=self.PRETRAINED_NAME,
            device=self.device,
        )
        self.model.eval()

    def embed_bytes(self, data: bytes) -> list[float]:
        image = self.Image.open(io.BytesIO(data)).convert("RGB")
        tensor = self.preprocess(image).unsqueeze(0).to(self.device)

        with self.torch.no_grad():
            vector = self.model.encode_image(tensor)
            vector = vector / vector.norm(dim=-1, keepdim=True)

        result = vector[0].detach().float().cpu().tolist()

        if len(result) != self.DIMENSION:
            raise RuntimeError(
                f"OpenCLIP trả vector {len(result)}D, cần {self.DIMENSION}D"
            )

        return result
