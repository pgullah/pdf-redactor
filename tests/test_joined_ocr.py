"""Character-aware field masking for multiple fields merged into one OCR word."""

import math
from dataclasses import replace
from io import BytesIO
from pathlib import Path

import pytesseract
import pytest
from PIL import ImageChops, ImageOps
from pypdf import PdfReader
from reportlab.pdfbase.pdfmetrics import getAscentDescent, stringWidth
from reportlab.pdfgen import canvas
from test_document_regression import mask_image, normalize, render_pages
from test_plugins import line

from app.redaction import Document, Finding, WordSlice
from app.redaction.engine import run_detector
from app.redaction.general import detect
from app.yaml_io import read_yaml, yaml_bytes
from main import main


def synthetic_document():
    raw = line("Name:Jane;Phone:12345 Date:2026", 0.05, 0.1, 1)
    for word in raw:
        word["characters"] = [
            {
                "text": c,
                "x": word["x"] + i * 0.009,
                "y": word["y"],
                "w": 0.007,
                "h": word["h"],
            }
            for i, c in enumerate(word["text"])
        ]
    return Document.from_ocr(
        "joined.pdf",
        "hash",
        [{"index": 0, "width": 1000, "height": 1000, "words": raw}],
    )


def test_joined_values_are_exact_spans_not_whole_words():
    document = synthetic_document()
    config = {
        "fields": {"name": {"labels": ["Name"]}, "phone": {"labels": ["Phone"]}},
        "stop_labels": ["Date"],
    }
    findings = list(detect(document, config))
    assert [(f.field, " ".join(w.text for w in f.words)) for f in findings] == [
        ("name", "Jane"),
        ("phone", "12345"),
    ]
    assert all(isinstance(f.words[0], WordSlice) for f in findings)
    masks, audit = run_detector("general", detect, document, config, 100)
    # Even unusually large padding cannot cover adjacent label characters.
    for mask, finding in zip(masks, findings, strict=True):
        span = finding.words[0]
        assert mask["x"] >= span.left_limit
        assert mask["x"] + mask["w"] <= span.right_limit + 1e-9
    assert all("start" in finding["words"][0] for finding in audit)


def test_forged_character_geometry_is_rejected():
    doc = synthetic_document()
    word = doc.pages[0].words[0]
    forged = replace(
        word, characters=tuple(replace(char, x=0) for char in word.characters)
    )

    def invalid(document, config):
        yield Finding("name", (WordSlice(forged, 5, 9),))

    with pytest.raises(ValueError, match="outside the OCR document"):
        run_detector("invalid", invalid, doc, {}, 3)


@pytest.mark.parametrize("missing_boxes", [False, True])
@pytest.mark.parametrize("mode", ["black", "text"])
def test_real_joined_token_export_preserves_labels_and_unselected_values(
    tmp_path, mode, missing_boxes, monkeypatch
):
    if missing_boxes:
        # Reproduce independent makebox OCR failing to align with TSV words.
        monkeypatch.setattr(
            pytesseract, "image_to_boxes", lambda *a, **kw: {"char": []}
        )
    try:
        pytesseract.get_tesseract_version()
    except pytesseract.TesseractNotFoundError:
        pytest.fail("Tesseract is required for merged-word validation")
    data = BytesIO()
    pdf = canvas.Canvas(data, pagesize=(720, 250))
    pdf.setFont("Helvetica", 20)
    # Tesseract reads the first three fields as one word on this controlled fixture.
    text = "Name:Jane;Phone:12345 Date:2026"
    pdf.drawString(35, 190, text)
    pdf.save()
    source = tmp_path / "input.pdf"
    source.write_bytes(data.getvalue())
    ocr = tmp_path / "ocr.yaml"
    assert main(["ocr", str(source), "-o", str(ocr)]) == 0
    words = read_yaml(ocr)["pages"][0]["words"]
    joined = next(w for w in words if "Jane" in w["text"] and "Phone" in w["text"])
    assert "".join(c["text"] for c in joined["characters"]) == joined["text"]
    config = tmp_path / "rules.yaml"
    config.write_bytes(
        yaml_bytes(
            {
                "general": {
                    "fields": {"name": {"labels": ["Name"]}},
                    "stop_labels": ["Phone", "Date"],
                },
                "masking": {
                    "default": {
                        "mode": mode,
                        **({"text": "XXXXX"} if mode == "text" else {}),
                    }
                },
            }
        )
    )
    plan = tmp_path / "masks.yaml"
    output = tmp_path / "output.pdf"
    assert main(["plan", str(source), "--config", str(config), "-o", str(plan)]) == 0
    assert main(["apply", str(source), "--masks", str(plan), "-o", str(output)]) == 0
    before = next(render_pages(source.read_bytes()))
    after = PdfReader(output).pages[0].images[0].image.convert("RGB")
    ascent, descent = getAscentDescent("Helvetica", 20)

    def region(start, end):
        return (
            math.floor(
                (35 + stringWidth(text[:start], "Helvetica", 20)) * before.width / 720
            ),
            math.floor((250 - 190 - ascent) * before.height / 250),
            math.ceil(
                (35 + stringWidth(text[:end], "Helvetica", 20)) * before.width / 720
            ),
            math.ceil((250 - 190 - descent) * before.height / 250),
        )

    def ink_stencil(spans):
        # Render expected glyphs separately from sensitive values, avoiding
        # typographic advance-width boxes that also contain adjacent whitespace.
        stencil = BytesIO()
        page = canvas.Canvas(stencil, pagesize=(720, 250))
        page.setFont("Helvetica", 20)
        for start, end in spans:
            page.drawString(
                35 + stringWidth(text[:start], "Helvetica", 20), 190, text[start:end]
            )
        page.save()
        return (
            next(render_pages(stencil.getvalue()))
            .convert("L")
            .point(lambda pixel: 255 if pixel < 200 else 0)
        )

    public_ink = ink_stencil([(0, 5), (9, len(text))])
    difference = ImageChops.difference(before, after).convert("L")
    assert ImageChops.multiply(difference, public_ink).getbbox() is None
    sensitive_ink = ink_stencil([(5, 9)])
    covered = mask_image(before.size, read_yaml(plan)["masks"], 0)
    assert ImageChops.subtract(sensitive_ink, covered).getbbox() is None
    readback = pytesseract.image_to_string(
        ImageOps.expand(after.crop(region(0, len(text))), border=12, fill="white"),
        config="--psm 7",
    )
    assert "Jane" not in readback
    assert normalize("Phone:12345") in normalize(readback)
    assert normalize("Date:2026") in normalize(readback)
    if mode == "text":
        assert "xxxxx" in normalize(readback)
    # Confirm the full redacted patch depends only on its style, not source pixels.
    mask = read_yaml(plan)["masks"][0]
    from app.redaction.rendering import paint_mask

    expected = before.copy()
    expected.paste("red", (0, 0, expected.width, expected.height))
    paint_mask(expected, mask)
    masked_box = (
        math.floor(mask["x"] * after.width),
        math.floor(mask["y"] * after.height),
        math.ceil((mask["x"] + mask["w"]) * after.width),
        math.ceil((mask["y"] + mask["h"]) * after.height),
    )
    assert (
        ImageChops.difference(
            expected.crop(masked_box), after.crop(masked_box)
        ).getbbox()
        is None
    )
    folder = Path("tmp/redaction-validation") / f"joined-token-{mode}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "input.pdf").write_bytes(source.read_bytes())
    (folder / "redacted.pdf").write_bytes(output.read_bytes())
    (folder / "masks.yaml").write_bytes(plan.read_bytes())
    preview = next(render_pages(output.read_bytes()))
    preview.thumbnail((1000, 400))
    preview.save(folder / "preview.png")


def test_hocr_recovery_rejects_wrong_text_or_location(monkeypatch):
    from PIL import Image

    from app.masker import attach_hocr_characters

    payload = b'<html><span class="ocrx_word"><span class="ocrx_cinfo" title="x_bboxes 10 20 20 30">A</span></span></html>'
    monkeypatch.setattr(pytesseract, "image_to_pdf_or_hocr", lambda *a, **kw: payload)
    matching = {"text": "A", "x": 0.1, "y": 0.2, "w": 0.1, "h": 0.1}
    wrong_text = dict(matching, text="B")
    wrong_location = dict(matching, x=0.5)
    with Image.new("RGB", (100, 100)) as image:
        attach_hocr_characters(image, "eng", [matching, wrong_text, wrong_location])
    assert matching["characters"][0]["text"] == "A"
    assert "characters" not in wrong_text
    assert "characters" not in wrong_location
