import math
import shutil
from contextlib import closing
from io import BytesIO
from pathlib import Path

import pypdfium2 as pdfium
import pytesseract
import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from app.masker import DPI, Store


def sample_pdf():
    """Digital text, a scanned page, rotated text, private metadata and attachment."""
    out = BytesIO()
    pdf = canvas.Canvas(out, pagesize=(420, 300))
    pdf.setTitle("SECRET metadata")
    pdf.setFont("Helvetica", 22)
    pdf.drawString(35, 220, "SECRET ACCOUNT 12345")
    pdf.drawString(35, 165, "PUBLIC INFORMATION")
    pdf.showPage()
    # Render a digital page to simulate a scanned source without a text layer.
    scan_source = BytesIO()
    c = canvas.Canvas(scan_source, pagesize=(420, 300))
    c.setFont("Helvetica", 22)
    c.drawString(35, 220, "SCANNED SECRET 67890")
    c.drawString(35, 165, "VISIBLE PUBLIC TEXT")
    c.save()
    with pdfium.PdfDocument(scan_source.getvalue()) as doc, closing(doc[0]) as page:
        bitmap = page.render(scale=DPI / 72)
        scan = bitmap.to_pil().convert("RGB")
        bitmap.close()
    pdf.drawImage(ImageReader(scan), 0, 0, width=420, height=300)
    scan.close()
    pdf.showPage()
    pdf.setPageSize((300, 420))
    pdf.setFont("Helvetica", 18)
    pdf.drawString(30, 350, "ROTATED SECRET")
    pdf.save()
    writer = PdfWriter(clone_from=PdfReader(BytesIO(out.getvalue())))
    writer.pages[2].rotate(90)
    writer.add_attachment("private.txt", b"SECRET attached data")
    final = BytesIO()
    writer.write(final)
    return final.getvalue()


@pytest.fixture
def store():
    workspace = Store()
    try:
        yield workspace
    finally:
        workspace.close()


def test_permanent_masks_remove_source_objects_and_pixels(store):
    doc = store.create(sample_pdf(), "example.pdf")
    masks = [{"page": index, "x": 0, "y": 0, "w": 1, "h": 0.4} for index in range(3)]
    exported = store.export(doc.id, masks)
    reader = PdfReader(BytesIO(exported))
    assert len(reader.pages) == 3
    assert not reader.attachments
    assert "/AcroForm" not in reader.trailer["/Root"]
    assert "SECRET" not in str(reader.metadata)
    for index, page in enumerate(reader.pages):
        assert page.extract_text() == ""
        assert not page.get("/Annots")
        assert len(page.images) == 1
        image = page.images[0].image.convert("RGB")
        masked = image.crop((0, 0, image.width, math.floor(image.height * 0.4)))
        assert masked.getextrema() == ((0, 0), (0, 0), (0, 0))
        assert float(page.mediabox.width) == pytest.approx(doc.pages[index]["width_pt"])
        assert float(page.mediabox.height) == pytest.approx(
            doc.pages[index]["height_pt"]
        )
        # Outside the mask remains pixel-identical to the rendered input.
        original = Image.open(doc.directory / f"{index}.png").convert("RGB")
        box = (0, math.ceil(image.height * 0.4) + 1, image.width, image.height)
        assert image.crop(box).tobytes() == original.crop(box).tobytes()
    output = Path("tmp/pdfs")
    output.mkdir(parents=True, exist_ok=True)
    (output / "sample.pdf").write_bytes(sample_pdf())
    (output / "redacted.pdf").write_bytes(exported)
    with pdfium.PdfDocument(exported) as result, closing(result[0]) as page:
        bitmap = page.render(scale=1.5)
        bitmap.to_pil().save(output / "redacted-preview.png")
        bitmap.close()


@pytest.mark.skipif(
    not shutil.which("tesseract"), reason="Tesseract executable not installed"
)
def test_real_ocr_on_digital_and_scanned_pages(store):
    doc = store.create(sample_pdf(), "example.pdf")
    masks = []
    for page in (0, 1):
        words = store.ocr(doc.id, page, "eng")
        assert any("SECRET" in w["text"] for w in words)
        for word in words:
            if "SECRET" in word["text"]:
                x = max(0, word["x"] - 0.005)
                y = max(0, word["y"] - 0.005)
                masks.append(
                    {
                        "page": page,
                        "x": x,
                        "y": y,
                        "w": word["w"] + 0.01,
                        "h": word["h"] + 0.01,
                    }
                )
    exported = store.export(doc.id, masks)
    reader = PdfReader(BytesIO(exported))
    for page in reader.pages[:2]:
        text = pytesseract.image_to_string(page.images[0].image)
        assert "SECRET" not in text
        assert "PUBLIC" in text


@pytest.mark.parametrize(
    "mask",
    [
        {"page": -1, "x": 0, "y": 0, "w": 0.1, "h": 0.1},
        {"page": 0, "x": 0.9, "y": 0, "w": 0.2, "h": 0.1},
        {"page": 0, "x": 0, "y": 0, "w": 0, "h": 0.1},
        {"page": 0.5, "x": 0, "y": 0, "w": 0.1, "h": 0.1},
    ],
)
def test_invalid_masks_rejected(store, mask):
    doc = store.create(sample_pdf(), "example.pdf")
    with pytest.raises(ValueError):
        store.export(doc.id, [mask])


def test_bad_files_pages_and_workspace_cleanup(store):
    with pytest.raises(ValueError, match="Cannot open"):
        store.create(b"not a PDF", "bad.pdf")
    assert not list(store.directory.iterdir())
    doc = store.create(sample_pdf(), "example.pdf")
    with pytest.raises(ValueError, match="Page does not exist"):
        store.ocr(doc.id, 999, "eng")
    with pytest.raises(ValueError, match="Invalid mask page"):
        store.export(doc.id, [{"page": 99, "x": 0, "y": 0, "w": 1, "h": 1}])
    store.close()
    assert not store.directory.exists()
    with pytest.raises(KeyError):
        store.get(doc.id)


def test_missing_tesseract_allows_manual_export(store, monkeypatch):
    doc = store.create(sample_pdf(), "example.pdf")

    def missing(*args, **kwargs):
        raise pytesseract.TesseractNotFoundError()

    monkeypatch.setattr(pytesseract, "image_to_data", missing)
    with pytest.raises(pytesseract.TesseractNotFoundError):
        store.ocr(doc.id, 0, "eng")
    exported = store.export(doc.id, [{"page": 0, "x": 0, "y": 0, "w": 1, "h": 0.5}])
    assert len(PdfReader(BytesIO(exported)).pages) == 3
