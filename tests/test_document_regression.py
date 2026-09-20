"""Real CLI/OCR regression corpus. Artifacts: tmp/redaction-validation/."""

import math
import re
import subprocess
import sys
from contextlib import closing
from io import BytesIO
from pathlib import Path

import pypdfium2 as pdfium
import pytesseract
import pytest
from PIL import Image, ImageChops, ImageDraw, ImageOps
from pypdf import PdfReader
from redaction_samples import make_sample
from test_redaction import sample_pdf

from app.masker import DPI
from app.yaml_io import read_yaml, yaml_bytes

ROOT = Path(__file__).parents[1]
ARTIFACTS = ROOT / "tmp" / "redaction-validation"
pytestmark = pytest.mark.document_regression


@pytest.fixture(scope="module", autouse=True)
def require_ocr():
    try:
        pytesseract.get_tesseract_version()
    except pytesseract.TesseractNotFoundError:
        pytest.fail(
            "Document regression tests require Tesseract; install it or set TESSERACT_CMD. These checks must not silently skip."
        )


def cli(*args):
    result = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), *map(str, args)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    return result.stderr


def normalize(text):
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def render_pages(data):
    with pdfium.PdfDocument(data) as pdf:
        pdf.init_forms()
        for index in range(len(pdf)):
            with closing(pdf[index]) as page:
                bitmap = page.render(scale=DPI / 72, draw_annots=True)
                try:
                    yield bitmap.to_pil().convert("RGB")
                finally:
                    bitmap.close()


def mask_image(size, masks, page):
    image = Image.new("L", size, 0)
    draw = ImageDraw.Draw(image)
    width, height = size
    for m in masks:
        if m["page"] == page:
            draw.rectangle(
                (
                    math.floor(m["x"] * width),
                    math.floor(m["y"] * height),
                    math.ceil((m["x"] + m["w"]) * width) - 1,
                    math.ceil((m["y"] + m["h"]) * height) - 1,
                ),
                fill=255,
            )
    return image


def pixel_box(box, size):
    w, h = size
    return (
        math.floor(box[0] * w),
        math.floor(box[1] * h),
        math.ceil(box[2] * w),
        math.ceil(box[3] * h),
    )


def check_export(source, output, masks, folder):
    reader = PdfReader(BytesIO(output))
    original = PdfReader(BytesIO(source))
    assert len(reader.pages) == len(original.pages)
    assert not reader.attachments
    assert "/AcroForm" not in reader.trailer["/Root"]
    assert "PRIVATE" not in str(reader.metadata) and "SECRET" not in str(
        reader.metadata
    )
    images = []
    for i, (before, page) in enumerate(
        zip(render_pages(source), reader.pages, strict=True)
    ):
        assert page.extract_text() == ""
        assert not page.get("/Annots")
        assert len(page.images) == 1
        after = page.images[0].image.convert("RGB")
        assert before.size == after.size
        with pdfium.PdfDocument(source) as doc, closing(doc[i]) as pdf_page:
            width, height = pdf_page.get_size()
        assert float(page.mediabox.width) == pytest.approx(width)
        assert float(page.mediabox.height) == pytest.approx(height)
        mask = mask_image(before.size, masks, i)
        # Independent checks of exported image content, not just OCR string absence.
        assert (
            Image.composite(after, Image.new("RGB", after.size), mask).getbbox() is None
        )
        difference = ImageChops.difference(before, after)
        assert (
            Image.composite(
                difference, Image.new("RGB", after.size), ImageChops.invert(mask)
            ).getbbox()
            is None
        )
        before.save(folder / f"page-{i + 1}-before.png")
        after.save(folder / f"page-{i + 1}-after.png")
        with pdfium.PdfDocument(output) as doc, closing(doc[i]) as rendered_page:
            bitmap = rendered_page.render(scale=1)
            try:
                bitmap.to_pil().save(folder / f"page-{i + 1}-export-preview.png")
            finally:
                bitmap.close()
        images.append((before, after, mask))
    return images


@pytest.mark.parametrize(
    "name",
    [
        "purchase_order_digital",
        "purchase_order_scanned",
        "purchase_order_shifted",
        "general_digital",
        "general_scanned",
    ],
)
def test_document_fields_are_removed_and_public_content_survives(name):
    sample = make_sample(name)
    folder = ARTIFACTS / name
    folder.mkdir(parents=True, exist_ok=True)
    source = folder / "input.pdf"
    source.write_bytes(sample.pdf)
    plan = folder / "masks.yaml"
    output = folder / "redacted.pdf"
    report = {
        "case": name,
        "status": "failed",
        "sensitive_values": [r.text for r in sample.sensitive],
        "preserved_values": [r.text for r in sample.preserved],
    }
    (folder / "result.yaml").write_bytes(yaml_bytes(report))
    cli("plan", source, "-o", plan, "--force", *sample.args)
    manifest = read_yaml(plan)
    assert {finding["field"] for finding in manifest["findings"]} == sample.fields
    cli("apply", source, "--masks", plan, "-o", output, "--force")
    images = check_export(sample.pdf, output.read_bytes(), manifest["masks"], folder)
    for region in sample.sensitive:
        before, after, mask = images[region.page]
        box = pixel_box(region.box, before.size)
        ink = before.crop(box).convert("L").point(lambda p: 255 if p < 200 else 0)
        assert ink.getbbox(), f"Fixture has no text at {region.text}"
        # Expected sensitive glyph pixels are defined by the fixture, independent of OCR boxes.
        uncovered = ImageChops.subtract(ink, mask.crop(box))
        assert uncovered.getbbox() is None, f"Unmasked sensitive pixels: {region.text}"
    before_text = [normalize(pytesseract.image_to_string(image[0])) for image in images]
    after_text = [normalize(pytesseract.image_to_string(image[1])) for image in images]
    for region in sample.sensitive:
        assert normalize(region.text) in before_text[region.page], (
            f"OCR positive control failed: {region.text}"
        )
        assert normalize(region.text) not in after_text[region.page], (
            f"Sensitive text survived: {region.text}"
        )
    for region in sample.preserved:
        before, after, mask = images[region.page]
        box = pixel_box(region.box, before.size)
        assert (
            ImageChops.difference(before.crop(box), after.crop(box)).getbbox() is None
        ), f"Public region changed: {region.text}"
        # Large black rectangles can make page OCR skip intact labels. OCR the
        # independently defined public crop as a second reading, while the exact
        # pixel comparison above still rejects even a single changed pixel.
        if normalize(region.text) not in after_text[region.page]:
            crop = ImageOps.expand(after.crop(box), border=12, fill="white")
            readback = pytesseract.image_to_string(crop, config="--psm 7")
            assert normalize(region.text) in normalize(readback), (
                f"Public text missing: {region.text}"
            )
    if name.endswith("scanned"):
        assert all(not page.extract_text() for page in PdfReader(source).pages)
    report.update(
        status="passed",
        mask_count=len(manifest["masks"]),
        checks=[
            "expected sensitive glyphs covered",
            "public regions unchanged",
            "source/output OCR comparison",
            "masked pixels black",
            "all pixels outside masks identical",
            "no source text or attachments",
            "page dimensions preserved",
        ],
    )
    (folder / "result.yaml").write_bytes(yaml_bytes(report))


def test_original_mixed_sample_ocr_and_manual_rotated_page():
    """Original digital/scanned/rotated sample; explicit review mask for sideways text."""
    folder = ARTIFACTS / "original_mixed"
    folder.mkdir(parents=True, exist_ok=True)
    source = folder / "input.pdf"
    source.write_bytes(sample_pdf())
    plan = folder / "masks.yaml"
    output = folder / "redacted.pdf"
    (folder / "result.yaml").write_bytes(
        yaml_bytes({"case": "original_mixed", "status": "failed"})
    )
    cli("plan", source, "-o", plan, "--text", "SECRET", "--force")
    manifest = read_yaml(plan)
    assert {0, 1} <= {m["page"] for m in manifest["masks"]}
    # The earlier example has sideways text on page 3. This is a reviewed manual
    # region, not a claim that automatic OCR orientation correction is supported.
    manifest["masks"].append({"page": 2, "x": 0, "y": 0, "w": 1, "h": 1})
    plan.write_bytes(yaml_bytes(manifest))
    cli("apply", source, "--masks", plan, "-o", output, "--force")
    images = check_export(
        source.read_bytes(), output.read_bytes(), manifest["masks"], folder
    )
    for before, after, _ in images[:2]:
        assert "SECRET" in pytesseract.image_to_string(before)
        text = pytesseract.image_to_string(after)
        assert "SECRET" not in text
        assert "PUBLIC" in text
    assert images[2][1].getbbox() is None
    (folder / "result.yaml").write_bytes(
        yaml_bytes(
            {
                "case": "original_mixed",
                "status": "passed",
                "note": "Pages 1–2: OCR text rule. Page 3: explicit whole-page manual mask for sideways text.",
            }
        )
    )


def test_public_only_document_does_not_produce_a_false_redaction():
    from reportlab.pdfgen import canvas

    folder = ARTIFACTS / "public_only"
    folder.mkdir(parents=True, exist_ok=True)
    source = folder / "input.pdf"
    pdf = canvas.Canvas(str(source), pagesize=(612, 792))
    pdf.setFont("Helvetica", 18)
    pdf.drawString(45, 700, "PUBLIC PRODUCT CATALOG")
    pdf.drawString(45, 650, "Widget quantity 12 price 25.00")
    pdf.save()
    (folder / "result.yaml").write_bytes(
        yaml_bytes({"case": "public_only", "status": "failed"})
    )
    for command, suffix in [("plan", "yaml"), ("redact", "pdf")]:
        output = folder / f"no-match.{suffix}"
        # These paths belong solely to this generated test case.
        output.unlink(missing_ok=True)
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "main.py"),
                command,
                str(source),
                "-o",
                str(output),
                "--plugin",
                "general",
                "--plugin-config",
                "examples/general.yaml",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
        assert result.returncode == 3, result.stderr
        assert not output.exists()
    (folder / "result.yaml").write_bytes(
        yaml_bytes(
            {
                "case": "public_only",
                "status": "passed",
                "checks": [
                    "No false field matches",
                    "exit code 3",
                    "no misleading output file",
                ],
            }
        )
    )
