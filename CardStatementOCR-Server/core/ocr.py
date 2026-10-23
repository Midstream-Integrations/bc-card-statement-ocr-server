"""docTR helpers shared by statement parsing (fallback) and receipt reading."""
from __future__ import annotations

import io
from functools import lru_cache

import numpy as np


@lru_cache(maxsize=1)
def model():
    from doctr.models import ocr_predictor

    return ocr_predictor(det_arch="db_resnet50", reco_arch="crnn_vgg16_bn", pretrained=True)


def words_to_lines(words: list[tuple[float, float, float, float, str]]) -> list[str]:
    """Group word boxes (x0, y0, x1, y1, text) into reading-order lines."""
    if not words:
        return []
    heights = sorted(w[3] - w[1] for w in words)
    tol = heights[len(heights) // 2] * 0.5
    rows: list[list] = []
    for w in sorted(words, key=lambda w: (w[1] + w[3]) / 2):
        yc = (w[1] + w[3]) / 2
        if rows and abs(yc - rows[-1][0]) <= tol:
            rows[-1][1].append(w)
            rows[-1][0] = sum((x[1] + x[3]) / 2 for x in rows[-1][1]) / len(rows[-1][1])
        else:
            rows.append([yc, [w]])
    return [" ".join(w[4] for w in sorted(r[1], key=lambda w: w[0])) for r in rows]


def _result_to_lines(result) -> list[list[str]]:
    pages = []
    for page in result.pages:
        words = []
        for block in page.blocks:
            for line in block.lines:
                for w in line.words:
                    (x0, y0), (x1, y1) = w.geometry
                    words.append((x0, y0, x1, y1, w.value))
        pages.append(words_to_lines(words))
    return pages


def lines_from_pdf(pdf_bytes: bytes, scale: float = 3.0) -> list[list[str]]:
    from doctr.io import DocumentFile

    return _result_to_lines(model()(DocumentFile.from_pdf(pdf_bytes, scale=scale)))


def lines_from_images(images: list[np.ndarray]) -> list[list[str]]:
    return _result_to_lines(model()(images))


def load_image(data: bytes) -> np.ndarray:
    """JPEG/PNG/HEIC -> upright RGB array (phone photos carry rotation in EXIF)."""
    from PIL import Image, ImageOps
    from pillow_heif import register_heif_opener

    register_heif_opener()
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    return np.array(img)
