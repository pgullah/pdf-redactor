"""Synthetic document fixtures with expected regions defined before OCR runs."""

from contextlib import closing
from dataclasses import dataclass, field
from io import BytesIO

import pypdfium2 as pdfium
from PIL import ImageFilter, ImageOps
from pypdf import PdfReader, PdfWriter
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import getAscentDescent, stringWidth
from reportlab.pdfgen import canvas

from app.masker import DPI


@dataclass
class Region:
    page: int
    text: str
    box: tuple[float, float, float, float]


@dataclass
class Sample:
    name: str
    pdf: bytes = b""
    args: list[str] = field(default_factory=list)
    sensitive: list[Region] = field(default_factory=list)
    preserved: list[Region] = field(default_factory=list)
    fields: set[str] = field(default_factory=set)


def make_sample(name):
    scanned = name.endswith("scanned")
    shifted = name == "purchase_order_shifted"
    is_po = name.startswith("purchase_order")
    width, height = (660, 850) if shifted else (612, 792)
    dx, dy = (17, -25) if shifted else (0, 0)
    sample = Sample(name)
    data = BytesIO()
    pdf = canvas.Canvas(data, pagesize=(width, height))
    pdf.setTitle("PRIVATE SOURCE METADATA")
    pdf.setAuthor("PRIVATE AUTHOR")
    pdf.setFont("Helvetica", 16)

    def draw(text, x, y, secret=False):
        x, y = x + dx, y + dy
        pdf.drawString(x, y, text)
        ascent, descent = getAscentDescent("Helvetica", 16)
        box = (
            x / width,
            (height - y - ascent) / height,
            (x + stringWidth(text, "Helvetica", 16)) / width,
            (height - y - descent) / height,
        )
        region = Region(0, text, box)
        (sample.sensitive if secret else sample.preserved).append(region)

    def inline(label, value, y):
        draw(label, 45, y)
        draw(value, 45 + stringWidth(label + " ", "Helvetica", 16), y, True)

    if is_po:
        sample.args = [
            "--plugin",
            "purchase-order",
            "--plugin-config",
            "examples/purchase-order.yaml",
        ]
        sample.fields = {
            "shipping_name_and_address",
            "billing_name_and_address",
            "buyer_name",
        }
        draw("Purchase Order", 45, 740)
        draw("Ship To:", 45, 680)
        draw("Bill To:", 335, 680)
        for text, x, y in [
            ("Jane Smith", 45, 657),
            ("123 Green Road", 45, 634),
            ("London", 45, 611),
            ("Acme Limited", 335, 657),
            ("45 Blue Street", 335, 634),
            ("Manchester", 335, 611),
        ]:
            draw(text, x, y, True)
        draw("Item Description", 45, 560)
        draw("Total 100.00", 335, 560)
        inline("Buyer:", "Alex Jones", 495)
        draw("Order Date: 2026-09-20", 45, 450)
    else:
        sample.args = [
            "--plugin",
            "general",
            "--plugin-config",
            "examples/general.yaml",
        ]
        sample.fields = {
            "person_name",
            "postal_address",
            "phone_number",
            "account_number",
            "email_address",
        }
        draw("Customer Information", 45, 740)
        inline("Full Name:", "Jane Smith", 680)
        draw("Postal Address:", 45, 630)
        draw("123 Green Road", 45, 607, True)
        draw("London", 45, 584, True)
        draw("Date: 2026-09-20", 45, 550)
        inline("Phone:", "020 1234 5678", 500)
        inline("Account Number:", "ACCT-987654", 450)
        inline("Email:", "jane@example.com", 400)
        draw("Total: 250.00", 45, 330)
        draw("Notes: Public information", 45, 280)
    pdf.save()
    content = data.getvalue()
    if scanned:
        # Image-only, grayscale, mildly blurred input. This is a distinct source type.
        out = BytesIO()
        scan_pdf = canvas.Canvas(out, pagesize=(width, height))
        with pdfium.PdfDocument(content) as doc, closing(doc[0]) as page:
            bitmap = page.render(scale=DPI / 72)
            try:
                image = ImageOps.grayscale(bitmap.to_pil()).filter(
                    ImageFilter.GaussianBlur(0.25)
                )
                scan_pdf.drawImage(ImageReader(image), 0, 0, width=width, height=height)
                image.close()
            finally:
                bitmap.close()
        scan_pdf.save()
        content = out.getvalue()
    # Include non-page secrets that must not survive image-only export.
    writer = PdfWriter(clone_from=PdfReader(BytesIO(content)))
    writer.add_metadata(
        {"/Title": "PRIVATE SOURCE METADATA", "/Author": "PRIVATE AUTHOR"}
    )
    writer.add_attachment("private.txt", b"PRIVATE ATTACHMENT Jane Smith")
    result = BytesIO()
    writer.write(result)
    sample.pdf = result.getvalue()
    return sample
