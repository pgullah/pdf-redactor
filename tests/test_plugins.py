import pytest
import yaml
from test_redaction import sample_pdf

from app.redaction import Document, Finding, Word
from app.redaction.engine import load_detector, run_detector
from app.redaction.purchase_order import detect
from main import main


def line(text, x, y, line_id):
    words = []
    for token in text.split():
        width = len(token) * 0.009
        words.append(
            {
                "text": token,
                "confidence": 95,
                "x": x,
                "y": y,
                "w": width,
                "h": 0.02,
                "block": 1,
                "paragraph": 1,
                "line": line_id,
            }
        )
        x += width + 0.008
    return words


def document(lines):
    words = [
        word for i, (text, x, y) in enumerate(lines) for word in line(text, x, y, i)
    ]
    return Document.from_ocr(
        "order.pdf",
        "hash",
        [{"index": 0, "width": 1000, "height": 1000, "words": words}],
    )


def text(findings):
    return {f.field: " ".join(w.text for w in f.words) for f in findings}


def test_po_columns_inline_name_and_section_boundaries():
    doc = document(
        [
            ("Purchase Order", 0.1, 0.05),
            ("Ship To:", 0.1, 0.15),
            ("Bill To:", 0.55, 0.15),
            ("Jane Smith", 0.1, 0.18),
            ("Acme Limited", 0.55, 0.18),
            ("123 Green Road", 0.1, 0.21),
            ("45 Blue Street", 0.55, 0.21),
            ("London AB1 2CD", 0.1, 0.24),
            ("London EF3 4GH", 0.55, 0.24),
            ("Item Description", 0.1, 0.27),
            ("Total 100.00", 0.55, 0.27),
            ("Buyer: Alex Jones", 0.1, 0.4),
            ("Order Date: 2026-09-20", 0.1, 0.43),
        ]
    )
    found = text(detect(doc, {}))
    assert found == {
        "ship_to": "Jane Smith 123 Green Road London AB1 2CD",
        "bill_to": "Acme Limited 45 Blue Street London EF3 4GH",
        "buyer_name": "Alex Jones",
    }
    masks, audit = run_detector("purchase-order", detect, doc, {}, 3)
    assert len(masks) == 18
    assert {entry["field"] for entry in audit} == set(found)
    assert "Jane" not in yaml.safe_dump(audit)


def test_po_custom_labels_and_value_right_of_label():
    doc = document(
        [("Recipient:", 0.1, 0.1), ("Jane Smith", 0.35, 0.1), ("Notes", 0.1, 0.2)]
    )
    config = {"fields": {"recipient": {"labels": ["Recipient"], "max_lines": 1}}}
    assert text(detect(doc, config)) == {"recipient": "Jane Smith"}
    assert list(detect(document([("No known fields here", 0.1, 0.1)]), {})) == []


def test_regex_maps_entity_spans_and_deduplicates():
    doc = document([("Email: jane@example.com", 0.1, 0.1)])

    def detector(doc, config):
        yield from doc.pages[0].find(r"jane@example\.com", field="email")
        yield from doc.pages[0].find("example", field="domain")

    masks, audit = run_detector("custom", detector, doc, {}, 3)
    assert len(masks) == 1
    assert len(audit) == 2
    assert doc.pages[0].lines[0].words_for_span(7, 23)[0].text == "jane@example.com"


def test_plugin_cannot_invent_word_geometry():
    doc = document([("Buyer: Jane", 0.1, 0.1)])

    def invalid(doc, config):
        yield Finding("name", (Word(0, 999, "invented", 99, 0, 0, 1, 1),))

    with pytest.raises(ValueError, match="outside the OCR document"):
        run_detector("invalid", invalid, doc, {}, 3)


def test_plugin_failure_and_invalid_output_fail_closed():
    doc = document([("Buyer: Jane", 0.1, 0.1)])

    def failure(doc, config):
        yield Finding("name", (doc.pages[0].words[-1],))
        raise RuntimeError("failed halfway")

    with pytest.raises(ValueError, match="failed halfway"):
        run_detector("broken", failure, doc, {}, 3)
    with pytest.raises(ValueError, match="Finding objects"):
        run_detector("bad", lambda *args: [None], doc, {}, 3)


def test_cli_file_plugin_config_composition_and_apply(tmp_path, monkeypatch):
    source = tmp_path / "order.pdf"
    source.write_bytes(sample_pdf())
    raw = line("Buyer: Jane Smith", 0.1, 0.1, 1) + line("PUBLIC", 0.1, 0.3, 2)
    monkeypatch.setattr("app.masker.Store.ocr", lambda *args: raw)
    plugin = tmp_path / "custom.py"
    plugin.write_text(
        'from app.redaction import Finding\ndef detect(document, config):\n    for page in document.pages:\n        yield from page.find(config["pattern"], field="buyer_name")\n'
    )
    spec = f"{plugin}:detect"
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({spec: {"pattern": "Jane Smith"}}))
    output = tmp_path / "plan.yaml"
    assert (
        main(
            [
                "plan",
                str(source),
                "-o",
                str(output),
                "--plugin",
                spec,
                "--plugin-config",
                str(config),
                "--text",
                "PUBLIC",
            ]
        )
        == 0
    )
    plan = yaml.safe_load(output.read_text())
    assert len(plan["masks"]) == 9
    assert {f["field"] for f in plan["findings"]} == {"text", "buyer_name"}
    assert (
        main(
            [
                "apply",
                str(source),
                "--masks",
                str(output),
                "-o",
                str(tmp_path / "out.pdf"),
            ]
        )
        == 0
    )
    plugin.write_text(
        'def detect(document, config):\n    raise RuntimeError("broken detector")\n'
    )
    output.unlink()
    assert main(["plan", str(source), "-o", str(output), "--plugin", spec]) == 1
    assert not output.exists()


def test_builtin_loading_and_config_validation():
    assert load_detector("purchase-order") is detect
    assert load_detector("app.redaction.purchase_order:detect") is detect
    with pytest.raises(ValueError, match="Cannot load"):
        load_detector("nonexistent_module:detect")
    with pytest.raises(ValueError, match="max_lines"):
        list(
            detect(
                document([]),
                {"fields": {"address": {"labels": ["Address"], "max_lines": 0}}},
            )
        )


def test_po_real_ocr_export(tmp_path):
    import shutil

    import pytesseract
    from pypdf import PdfReader
    from reportlab.pdfgen import canvas

    if not shutil.which("tesseract"):
        pytest.skip("Tesseract not installed")
    source = tmp_path / "purchase-order.pdf"
    pdf = canvas.Canvas(str(source), pagesize=(612, 792))
    pdf.setFont("Helvetica", 16)
    for text, x, y in [
        ("Purchase Order", 45, 740),
        ("Ship To:", 45, 680),
        ("Bill To:", 335, 680),
        ("Jane Smith", 45, 657),
        ("Acme Limited", 335, 657),
        ("123 Green Road", 45, 634),
        ("45 Blue Street", 335, 634),
        ("London", 45, 611),
        ("Manchester", 335, 611),
        ("Item Description", 45, 560),
        ("Total 100.00", 335, 560),
    ]:
        pdf.drawString(x, y, text)
    pdf.save()
    plan = tmp_path / "plan.yaml"
    assert (
        main(["plan", str(source), "-o", str(plan), "--plugin", "purchase-order"]) == 0
    )
    decisions = yaml.safe_load(plan.read_text())["findings"]
    assert {f["field"] for f in decisions} == {"ship_to", "bill_to"}
    output = tmp_path / "redacted.pdf"
    assert main(["apply", str(source), "--masks", str(plan), "-o", str(output)]) == 0
    text = pytesseract.image_to_string(PdfReader(output).pages[0].images[0].image)
    assert "Jane" not in text and "Acme" not in text
    assert "Green" not in text and "Blue" not in text
    assert "London" not in text and "Manchester" not in text
    assert "Purchase Order" in text and "Item Description" in text
