"""Neighboring labels on one OCR line must bound each field's value."""

import pytest
from test_plugins import document, text

from app.redaction.general import detect
from app.redaction.purchase_order import detect as detect_po


def test_multiple_target_fields_and_public_stop_on_one_line():
    doc = document([("Name: Jane Smith Phone: 020 1234 Date: 2026-09-21", 0.05, 0.1)])
    config = {
        "fields": {
            "name": {"labels": ["Name"], "max_lines": 1},
            "phone": {"labels": ["Phone"], "max_lines": 1},
        },
        "stop_labels": ["Date"],
    }
    assert text(detect(doc, config)) == {"name": "Jane Smith", "phone": "020 1234"}


def test_only_requested_field_is_selected_after_public_prefix():
    doc = document([("Reference: PUBLIC Name: Jane Smith Date: 2026", 0.05, 0.1)])
    config = {
        "fields": {"name": {"labels": ["Name"]}},
        "stop_labels": ["Reference", "Date"],
    }
    assert text(detect(doc, config)) == {"name": "Jane Smith"}


def test_longest_label_repeated_fields_and_separators():
    doc = document([("Full Name: Jane | Date: 2026 ; Full Name: Alex", 0.05, 0.1)])
    config = {
        "fields": {"name": {"labels": ["Name", "Full Name"]}},
        "stop_labels": ["Date"],
    }
    assert [
        " ".join(word.text for word in finding.words) for finding in detect(doc, config)
    ] == ["Jane", "Alex"]


def test_value_words_that_resemble_labels_are_not_boundaries():
    doc = document([("Name: Mary Date Jones Phone: 12345", 0.05, 0.1)])
    config = {
        "fields": {"name": {"labels": ["Name"]}},
        "stop_labels": ["Date", "Phone"],
    }
    assert text(detect(doc, config)) == {"name": "Mary Date Jones"}


def test_po_inline_fields_keep_order_date_visible():
    doc = document([("Buyer: Jane Supplier Name: Acme Order Date: 2026", 0.05, 0.1)])
    assert text(detect_po(doc, {})) == {"buyer_name": "Jane", "supplier_name": "Acme"}


def test_multiline_address_does_not_take_adjacent_column():
    doc = document(
        [
            ("Address:", 0.05, 0.1),
            ("Date: 2026", 0.55, 0.1),
            ("123 Green Road", 0.05, 0.13),
            ("Reference: PUBLIC", 0.55, 0.13),
            ("London", 0.05, 0.16),
            ("Notes: Keep", 0.55, 0.16),
            ("Total: 100.00", 0.05, 0.19),
        ]
    )
    config = {
        "fields": {"address": {"labels": ["Address"], "max_lines": 5}},
        "stop_labels": ["Date", "Reference", "Notes", "Total"],
    }
    assert text(detect(doc, config)) == {"address": "123 Green Road London"}


def test_joined_value_without_character_boxes_requires_review():
    doc = document([("Name: Jane|Phone: 12345", 0.05, 0.1)])
    config = {"fields": {"name": {"labels": ["Name"]}}, "stop_labels": ["Phone"]}
    with pytest.raises(ValueError, match="Cannot locate characters"):
        list(detect(doc, config))


def test_colonless_label_after_explicit_separator():
    doc = document([("Name Jane Smith | Date 2026", 0.05, 0.1)])
    config = {"fields": {"name": {"labels": ["Name"]}}, "stop_labels": ["Date"]}
    assert text(detect(doc, config)) == {"name": "Jane Smith"}


@pytest.mark.parametrize("scanned", [False, True], ids=["digital", "scanned"])
@pytest.mark.parametrize("mode", ["black", "text"])
def test_real_inline_fields_export(tmp_path, scanned, mode):
    import math
    from io import BytesIO
    from pathlib import Path

    import pytesseract
    from PIL import ImageChops, ImageOps
    from pypdf import PdfReader
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfbase.pdfmetrics import getAscentDescent, stringWidth
    from reportlab.pdfgen import canvas
    from test_document_regression import mask_image, normalize, render_pages

    from app.yaml_io import read_yaml, yaml_bytes
    from main import main

    try:
        pytesseract.get_tesseract_version()
    except pytesseract.TesseractNotFoundError:
        pytest.fail("Tesseract required for inline-field export validation")
    data = BytesIO()
    pdf = canvas.Canvas(data, pagesize=(720, 360))
    pdf.setFont("Helvetica", 16)
    sensitive, public = [], []
    for y, tokens in [
        (
            290,
            [
                ("Name: ", False),
                ("Jane Smith", True),
                ("   Phone: ", False),
                ("020 1234 5678", True),
                ("   Date: 2026-09-21", False),
            ],
        ),
        (
            235,
            [
                ("Address: ", False),
                ("123 Green Road", True),
                ("   Reference: PUBLIC-42", False),
            ],
        ),
        (170, [("Notes: Keep this visible", False)]),
    ]:
        x = 35
        for token, secret in tokens:
            pdf.drawString(x, y, token)
            width = stringWidth(token, "Helvetica", 16)
            ascent, descent = getAscentDescent("Helvetica", 16)
            # Exclude separating whitespace from public glyph boxes, allowing the
            # normal 3-pixel padding around adjoining redacted words.
            left = x + stringWidth(
                token[: len(token) - len(token.lstrip())], "Helvetica", 16
            )
            right = x + stringWidth(token.rstrip(), "Helvetica", 16)
            (sensitive if secret else public).append(
                (token.strip(), (left, 360 - y - ascent, right, 360 - y - descent))
            )
            x += width
    pdf.save()
    content = data.getvalue()
    if scanned:
        scan = next(render_pages(content))
        output = BytesIO()
        pdf = canvas.Canvas(output, pagesize=(720, 360))
        pdf.drawImage(ImageReader(scan), 0, 0, width=720, height=360)
        pdf.save()
        content = output.getvalue()
    source = tmp_path / "input.pdf"
    source.write_bytes(content)
    config = tmp_path / "rules.yaml"
    config.write_bytes(
        yaml_bytes(
            {
                "masking": {
                    "default": {
                        "mode": mode,
                        **({"text": "XXXXX"} if mode == "text" else {}),
                    }
                },
                "general": {
                    "fields": {
                        "name": {"labels": ["Name"]},
                        "phone": {"labels": ["Phone"]},
                        "address": {"labels": ["Address"], "max_lines": 3},
                    },
                    "stop_labels": ["Date", "Reference", "Notes"],
                },
            }
        )
    )
    plan, output = tmp_path / "masks.yaml", tmp_path / "redacted.pdf"
    assert main(["plan", str(source), "--config", str(config), "-o", str(plan)]) == 0
    assert main(["apply", str(source), "--masks", str(plan), "-o", str(output)]) == 0
    masks = read_yaml(plan)["masks"]
    before = next(render_pages(content))
    reader = PdfReader(output)
    after = reader.pages[0].images[0].image.convert("RGB")
    assert not reader.pages[0].extract_text()
    mask = mask_image(before.size, masks, 0)
    source_text = normalize(pytesseract.image_to_string(before))
    result_text = normalize(pytesseract.image_to_string(after))

    def box(coords):
        return (
            math.floor(coords[0] * before.width / 720),
            math.floor(coords[1] * before.height / 360),
            math.ceil(coords[2] * before.width / 720),
            math.ceil(coords[3] * before.height / 360),
        )

    for value, coords in sensitive:
        assert normalize(value) in source_text
        assert normalize(value) not in result_text
        rect = box(coords)
        ink = before.crop(rect).convert("L").point(lambda p: 255 if p < 200 else 0)
        assert ImageChops.subtract(ink, mask.crop(rect)).getbbox() is None, value
    for value, coords in public:
        rect = box(coords)
        assert (
            ImageChops.difference(before.crop(rect), after.crop(rect)).getbbox() is None
        ), value
        readback = pytesseract.image_to_string(
            ImageOps.expand(after.crop(rect), border=12, fill="white"), config="--psm 7"
        )
        assert normalize(value) in normalize(readback), value
    if mode == "text":
        assert "XXXXX" in pytesseract.image_to_string(after)
    artifacts = (
        Path("tmp/redaction-validation")
        / f"inline-{mode}-{'scanned' if scanned else 'digital'}"
    )
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "input.pdf").write_bytes(content)
    (artifacts / "redacted.pdf").write_bytes(output.read_bytes())
    (artifacts / "masks.yaml").write_bytes(plan.read_bytes())
    preview = next(render_pages(output.read_bytes()))
    preview.thumbnail((1000, 600))
    preview.save(artifacts / "preview.png")
