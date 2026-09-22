"""Image-only redaction pipeline. Coordinates are normalized to the rendered page."""

from __future__ import annotations

import math
import os
import re
import shutil
import tempfile
import threading
import uuid
import xml.etree.ElementTree as ET
from contextlib import closing
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

import pypdfium2 as pdfium
import pytesseract
from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from app.redaction.rendering import paint_mask, validate_mask

DPI = 200
MAX_BYTES = 50 * 1024 * 1024
MAX_PAGES = 100
MAX_PAGE_PIXELS = 25_000_000
MAX_TOTAL_PIXELS = 250_000_000
# PDFium is not thread safe; all operations on this store share this lock.
LOCK = threading.RLock()
if os.environ.get("TESSERACT_CMD"):
    pytesseract.pytesseract.tesseract_cmd = os.environ["TESSERACT_CMD"]


@dataclass
class Document:
    id: str
    name: str
    directory: Path
    pages: list[dict]
    words: dict = field(default_factory=dict)


class Store:
    def __init__(self):
        self.directory = Path(tempfile.mkdtemp(prefix="pdf-masker-"))
        self.documents: dict[str, Document] = {}

    def close(self):
        with LOCK:
            shutil.rmtree(self.directory, ignore_errors=True)
            self.documents.clear()

    def get(self, key):
        with LOCK:
            if key not in self.documents:
                raise KeyError(
                    "Document is not loaded or its workspace has been closed."
                )
            doc = self.documents[key]
            return doc

    def create(self, data: bytes, name: str):
        with LOCK:
            if not data or len(data) > MAX_BYTES:
                raise ValueError("Choose a PDF up to 50 MB.")
            key = uuid.uuid4().hex
            directory = self.directory / key
            directory.mkdir(mode=0o700)
            try:
                try:
                    pdf = pdfium.PdfDocument(data)
                except Exception as exc:
                    raise ValueError(
                        "Cannot open this PDF. Check that it is valid and not password protected."
                    ) from exc
                with pdf:
                    if not 1 <= len(pdf) <= MAX_PAGES:
                        raise ValueError("PDF must contain between 1 and 100 pages.")
                    pdf.init_forms()
                    pages = []
                    total_pixels = 0
                    for index in range(len(pdf)):
                        with closing(pdf[index]) as page:
                            width, height = page.get_size()
                            if not all(
                                math.isfinite(v) and v > 0 for v in (width, height)
                            ):
                                raise ValueError("PDF has invalid page dimensions.")
                            pixels = math.ceil(width * DPI / 72) * math.ceil(
                                height * DPI / 72
                            )
                            total_pixels += pixels
                            if (
                                pixels > MAX_PAGE_PIXELS
                                or total_pixels > MAX_TOTAL_PIXELS
                            ):
                                raise ValueError(
                                    "PDF exceeds the rendering limit. Split it into smaller documents."
                                )
                            bitmap = page.render(scale=DPI / 72, draw_annots=True)
                            try:
                                image = bitmap.to_pil().convert("RGB")
                                image.save(directory / f"{index}.png")
                                pages.append(
                                    {
                                        "index": index,
                                        "width": image.width,
                                        "height": image.height,
                                        "width_pt": width,
                                        "height_pt": height,
                                    }
                                )
                                image.close()
                            finally:
                                bitmap.close()
                doc = Document(key, name, directory, pages)
                self.documents[key] = doc
                return doc
            except Exception:
                shutil.rmtree(directory, ignore_errors=True)
                raise

    def ocr(self, key: str, page: int, language: str):
        with LOCK:
            doc = self.get(key)
            if not 0 <= page < len(doc.pages):
                raise ValueError("Page does not exist.")
            cache_key = (page, language)
            if cache_key in doc.words:
                return doc.words[cache_key]
            with Image.open(doc.directory / f"{page}.png") as image:
                data = pytesseract.image_to_data(
                    image,
                    lang=language,
                    output_type=pytesseract.Output.DICT,
                    timeout=120,
                )
                words = []
                for i, text in enumerate(data["text"]):
                    if not text.strip():
                        continue
                    words.append(
                        {
                            "id": len(words),
                            "text": text,
                            "confidence": float(data["conf"][i]),
                            "block": int(data["block_num"][i]),
                            "paragraph": int(data["par_num"][i]),
                            "line": int(data["line_num"][i]),
                            "x": data["left"][i] / image.width,
                            "y": data["top"][i] / image.height,
                            "w": data["width"][i] / image.width,
                            "h": data["height"][i] / image.height,
                        }
                    )
                # Word-level TSV may merge values with labels, e.g. Jane|Phone:.
                # Only request glyph boxes on pages with potentially joined tokens.
                candidates = [
                    word
                    for word in words
                    if any(c in word["text"][1:-1] for c in ":|;,/()-")
                    or word["text"].endswith(("|", ";"))
                ]
                if candidates:
                    boxes = pytesseract.image_to_boxes(
                        image,
                        lang=language,
                        output_type=pytesseract.Output.DICT,
                        timeout=120,
                    )
                    glyphs = []
                    for i, char in enumerate(boxes.get("char", [])):
                        left, right = int(boxes["left"][i]), int(boxes["right"][i])
                        top, bottom = (
                            image.height - int(boxes["top"][i]),
                            image.height - int(boxes["bottom"][i]),
                        )
                        glyphs.append(
                            {
                                "text": char,
                                "x": left / image.width,
                                "y": top / image.height,
                                "w": (right - left) / image.width,
                                "h": (bottom - top) / image.height,
                            }
                        )
                    for word in candidates:
                        chars = [
                            char
                            for char in glyphs
                            if word["x"] - 1 / image.width
                            <= char["x"] + char["w"] / 2
                            <= word["x"] + word["w"] + 1 / image.width
                            and word["y"] - 1 / image.height
                            <= char["y"] + char["h"] / 2
                            <= word["y"] + word["h"] + 1 / image.height
                        ]
                        if (
                            len(chars) == len(word["text"])
                            and "".join(c["text"] for c in chars) == word["text"]
                        ):
                            word["characters"] = chars
                    missing = [word for word in candidates if "characters" not in word]
                    if missing:
                        attach_hocr_characters(image, language, missing)
            doc.words[cache_key] = words
            return words

    def export(self, key: str, masks: list[dict]):
        with LOCK:
            doc = self.get(key)
            grouped = {}
            for mask in masks:
                validate_mask(mask, len(doc.pages))
                grouped.setdefault(mask["page"], []).append(mask)
            output = BytesIO()
            pdf = canvas.Canvas(output, pageCompression=1)
            pdf.setTitle("Redacted document")
            pdf.setAuthor("")
            pdf.setSubject("")
            # Never import source PDF objects, original images, metadata or OCR text.
            for page in doc.pages:
                with Image.open(doc.directory / f"{page['index']}.png") as original:
                    image = original.convert("RGB")
                    # Solid black wins overlaps; other styles follow mask order.
                    ordered = sorted(
                        grouped.get(page["index"], []),
                        key=lambda mask: (
                            mask.get("style", {}).get("mode", "black") == "black"
                        ),
                    )
                    for mask in ordered:
                        paint_mask(image, mask)
                    pdf.setPageSize((page["width_pt"], page["height_pt"]))
                    pdf.drawImage(
                        ImageReader(image),
                        0,
                        0,
                        width=page["width_pt"],
                        height=page["height_pt"],
                    )
                    pdf.showPage()
                    image.close()
            pdf.save()
            return output.getvalue()


def attach_hocr_characters(image, language, words):
    """Recover aligned glyphs from one recognition result, without guessing widths.

    Unlike makebox output, hOCR nests character boxes inside their owning words.
    Match both text and location before accepting a recovery for a TSV word.
    """
    payload = pytesseract.image_to_pdf_or_hocr(
        image,
        lang=language,
        extension="hocr",
        config="-c hocr_char_boxes=1",
        timeout=120,
    )
    root = ET.fromstring(payload)
    for node in root.iter():
        if "ocrx_word" not in node.get("class", "").split():
            continue
        chars = []
        for glyph in node.iter():
            if "ocrx_cinfo" not in glyph.get("class", "").split():
                continue
            match = re.search(
                r"\bx_bboxes (\d+) (\d+) (\d+) (\d+)", glyph.get("title", "")
            )
            text = "".join(glyph.itertext())
            if not match or len(text) != 1:
                chars = []
                break
            left, top, right, bottom = map(int, match.groups())
            chars.append(
                {
                    "text": text,
                    "x": left / image.width,
                    "y": top / image.height,
                    "w": (right - left) / image.width,
                    "h": (bottom - top) / image.height,
                }
            )
        if not chars:
            continue
        text = "".join(c["text"] for c in chars)
        for word in words:
            if word["text"] != text:
                continue
            # Compare all four edges, not just centers: duplicate values can
            # occur elsewhere on the page, and partial matches are unsafe.
            bounds = (
                min(c["x"] for c in chars),
                min(c["y"] for c in chars),
                max(c["x"] + c["w"] for c in chars),
                max(c["y"] + c["h"] for c in chars),
            )
            expected = (
                word["x"],
                word["y"],
                word["x"] + word["w"],
                word["y"] + word["h"],
            )
            if all(
                abs(a - b) <= tolerance
                for a, b, tolerance in zip(
                    bounds, expected, (2 / image.width, 2 / image.height) * 2
                )
            ):
                word["characters"] = chars
