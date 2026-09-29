"""OCR for scanned PDFs and images (Tesseract via pytesseract).

Words are grouped into lines; a wide horizontal gap inside a line is written as `` | `` so that scanned
*tables* keep their column structure for the text parser. The mean word confidence of every line is kept.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from statistics import median
from typing import Iterator

from .base import BaseReader, Block, FileReadError, ReadOptions, TextBlock, UnsupportedFileError

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp")


def ocr_available(tesseract_cmd: str = "") -> bool:
    if tesseract_cmd:
        return Path(tesseract_cmd).exists()
    return shutil.which("tesseract") is not None


def _prepare(img):
    from PIL import ImageOps

    img = ImageOps.exif_transpose(img)
    img = img.convert("L")
    if img.width < 1400:                       # small scans OCR better when enlarged
        factor = 2 if img.width >= 700 else 3
        img = img.resize((img.width * factor, img.height * factor))
    return ImageOps.autocontrast(img)


def ocr_image(img, options: ReadOptions) -> list[tuple[int, str, dict]]:
    """Run Tesseract on a PIL image -> [(line_no, text, {'ocr_confidence': x})]."""
    import pytesseract

    if options.tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = options.tesseract_cmd
    if not ocr_available(options.tesseract_cmd):
        raise UnsupportedFileError(
            "OCR requested but Tesseract is not installed (install it, e.g. `apt install tesseract-ocr`, "
            "or set TESSERACT_CMD)."
        )
    prepared = _prepare(img)
    try:
        data = pytesseract.image_to_data(
            prepared, lang=options.ocr_lang, config="--psm 6", output_type=pytesseract.Output.DICT
        )
    except pytesseract.TesseractError as exc:
        raise FileReadError(f"Tesseract failed: {exc}") from exc

    lines: dict[tuple[int, int, int], list[dict]] = {}
    for i, text in enumerate(data["text"]):
        text = (text or "").strip()
        if not text:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(
            {"t": text, "l": data["left"][i], "r": data["left"][i] + data["width"][i],
             "h": data["height"][i], "c": float(data["conf"][i])}
        )
    out: list[tuple[int, str, dict]] = []
    for n, key in enumerate(sorted(lines), start=1):
        words = sorted(lines[key], key=lambda w: w["l"])
        heights = [w["h"] for w in words if w["h"] > 0]
        gap_limit = 1.6 * (median(heights) if heights else 20)
        parts = [words[0]["t"]]
        for prev, cur in zip(words, words[1:]):
            parts.append(" | " if cur["l"] - prev["r"] > gap_limit else " ")
            parts.append(cur["t"])
        confs = [w["c"] for w in words if w["c"] >= 0]
        out.append((n, "".join(parts), {"ocr_confidence": round(sum(confs) / len(confs), 1) if confs else None}))
    return out


class ImageReader(BaseReader):
    extensions = IMAGE_EXTENSIONS

    def read(self, path: Path) -> Iterator[Block]:
        from PIL import Image, ImageSequence

        if self.options.inspect or not self.options.ocr:
            self.meta.update(pages=1, needs_ocr=True)
            if not self.options.inspect:
                self.issue("image", "image not read: OCR is disabled in settings", "warning")
            return
        try:
            with Image.open(path) as im:
                frames = [f.copy() for f in ImageSequence.Iterator(im)]
        except Exception as exc:
            raise FileReadError(f"Cannot open image: {type(exc).__name__}: {exc}") from exc
        self.meta.update(pages=len(frames), ocr_pages=len(frames))
        for page_no, frame in enumerate(frames, start=1):
            lines = ocr_image(frame, self.options)
            confs = [c["ocr_confidence"] for _, _, c in lines if c.get("ocr_confidence") is not None]
            if confs and sum(confs) / len(confs) < 60:
                self.issue(f"page {page_no}", f"low OCR confidence (mean {sum(confs)/len(confs):.0f}%): verify results", "warning")
            yield TextBlock(lines=lines, page=page_no, method="ocr_image")
