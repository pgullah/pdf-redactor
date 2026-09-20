"""Example custom detector: uv run python main.py plan order.pdf -o masks.yaml
--plugin examples/vendor_po.py:detect
"""

from app.redaction import Finding
from app.redaction.purchase_order import detect as detect_fields


def detect(document, config):
    # Routing can use any OCR content, vendor identifiers, page count, or layout.
    if "purchase order" not in document.text.casefold():
        raise ValueError("Expected a purchase order; refusing to use this detector.")

    yield from detect_fields(
        document,
        {
            "fields": {
                "recipient_name_and_address": {
                    "labels": config.get("recipient_labels", ["Deliver To", "Ship To"]),
                    "max_lines": 5,
                },
                "contact_name": {"labels": ["Contact", "Ordered By"], "max_lines": 1},
            }
        },
    )
    # Your own logic can use Page.find(), Line.words_for_span(), and Finding.
    # A local NER model can return spans and map them back to OCR words here.
    for page in document.pages:
        for finding in page.find(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", field="email"):
            yield Finding(
                "email", finding.words, "Email address in this purchase order"
            )
