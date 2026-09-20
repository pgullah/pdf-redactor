from pathlib import Path

import pytesseract
import pytest
from PIL import Image, ImageChops
from pypdf import PdfReader
from redaction_samples import make_sample
from test_plugins import document

from app.masker import Store
from app.redaction.engine import run_detector
from app.redaction.rendering import normalize_style, paint_mask, parse_masking
from app.yaml_io import read_yaml, yaml_bytes
from main import main


def test_replacement_is_independent_of_original_pixels_and_clipped():
    original = Image.new("RGB", (300, 100), "red")
    other = Image.new("RGB", (300, 100), "blue")
    mask = {
        "page": 0,
        "x": 0.1,
        "y": 0.2,
        "w": 0.6,
        "h": 0.5,
        "style": {"mode": "text", "text": "XXXXX"},
    }
    paint_mask(original, mask)
    paint_mask(other, mask)
    box = (30, 20, 210, 70)
    assert ImageChops.difference(original.crop(box), other.crop(box)).getbbox() is None
    assert original.getpixel((29, 19)) == (255, 0, 0)
    assert other.getpixel((210, 70)) == (0, 0, 255)
    assert original.crop(box).convert("L").getextrema() == (0, 255)


def test_default_black_and_fill_pixels():
    for style, color in [
        (None, (0, 0, 0)),
        ({"mode": "fill", "background": "#123456"}, (18, 52, 86)),
    ]:
        image = Image.new("RGB", (100, 100), "white")
        mask = {"page": 0, "x": 0, "y": 0, "w": 0.5, "h": 0.5}
        if style:
            mask["style"] = style
        paint_mask(image, mask)
        assert image.crop((0, 0, 50, 50)).getextrema() == tuple((c, c) for c in color)
        assert image.getpixel((50, 50)) == (255, 255, 255)


@pytest.mark.parametrize(
    "style",
    [
        {"mode": "blur"},
        {"mode": []},
        {"mode": "black", "text": "ignored"},
        {"mode": "text", "text": ""},
        {"mode": "text", "text": "two\nlines"},
        {"mode": "text", "background": "transparent"},
        {"mode": "text", "foreground": "#FFFFFF"},
        {"mode": "text", "text": "名字"},
        {"mode": "text", "font_path": "/missing/font.ttf"},
    ],
)
def test_invalid_styles_are_errors(style):
    with pytest.raises(ValueError):
        normalize_style(style)


def test_field_override_and_line_grouping():
    doc = document([("Name: Jane Smith", 0.1, 0.1), ("Address: Green Road", 0.1, 0.3)])

    def detector(doc, config):
        yield from doc.pages[0].find("Jane Smith", field="person_name")
        yield from doc.pages[0].find("Green Road", field="address")

    config = parse_masking(
        {
            "default": {"mode": "text", "text": "REMOVED"},
            "fields": {
                "person_name": {"mode": "fill", "background": "#CCCCCC"},
                "custom.person_name": {"mode": "text", "text": "XXXXX"},
                "address": {"mode": "black"},
            },
        },
        Path.cwd(),
    )
    masks, _ = run_detector("custom", detector, doc, {}, 3, config)
    assert (
        len(masks) == 3
    )  # Name is one contiguous span; address retains two black word masks.
    assert masks[0]["style"]["text"] == "XXXXX"
    assert all("style" not in m for m in masks[1:])


def test_region_only_without_ocr_and_black_overlap(tmp_path, monkeypatch):
    sample = make_sample("general_digital")
    source = tmp_path / "input.pdf"
    source.write_bytes(sample.pdf)
    config = tmp_path / "regions.yaml"
    config.write_bytes(
        yaml_bytes(
            {
                "masking": {
                    "regions": [
                        {"page": 0, "x": 0.1, "y": 0.1, "w": 0.5, "h": 0.1},
                        {
                            "page": 0,
                            "x": 0.1,
                            "y": 0.1,
                            "w": 0.5,
                            "h": 0.1,
                            "style": {"mode": "text", "text": "XXXXX"},
                        },
                    ]
                }
            }
        )
    )
    monkeypatch.setattr(
        Store, "ocr", lambda *args: pytest.fail("Region-only masking must not run OCR")
    )
    plan = tmp_path / "plan.yaml"
    output = tmp_path / "output.pdf"
    assert main(["plan", str(source), "--config", str(config), "-o", str(plan)]) == 0
    assert read_yaml(plan)["version"] == 2
    assert main(["apply", str(source), "--masks", str(plan), "-o", str(output)]) == 0
    image = PdfReader(output).pages[0].images[0].image.convert("RGB")
    assert image.crop(
        (
            int(image.width * 0.11),
            int(image.height * 0.11),
            int(image.width * 0.59),
            int(image.height * 0.19),
        )
    ).getextrema() == ((0, 0), (0, 0), (0, 0))


def test_real_ocr_text_replacement_plan_and_apply(tmp_path):
    try:
        pytesseract.get_tesseract_version()
    except pytesseract.TesseractNotFoundError:
        pytest.skip("Tesseract not installed")
    sample = make_sample("general_digital")
    source = tmp_path / "input.pdf"
    source.write_bytes(sample.pdf)
    plan = tmp_path / "plan.yaml"
    output = tmp_path / "redacted.pdf"
    config = Path(__file__).parents[1] / "examples" / "masking.yaml"
    assert (
        main(
            [
                "plan",
                str(source),
                "-o",
                str(plan),
                "--plugin",
                "general",
                "--config",
                str(config),
            ]
        )
        == 0
    )
    manifest = read_yaml(plan)
    assert manifest["version"] == 2
    assert any(
        mask.get("style", {}).get("text") == "XXXXX" for mask in manifest["masks"]
    )
    assert main(["apply", str(source), "--masks", str(plan), "-o", str(output)]) == 0
    reader = PdfReader(output)
    assert reader.pages[0].extract_text() == ""
    assert not reader.attachments
    image = reader.pages[0].images[0].image
    text = pytesseract.image_to_string(image)
    assert "Jane" not in text and "Smith" not in text
    assert "Green" not in text and "London" not in text
    assert "jane@example.com" not in text
    assert "987654" not in text
    assert "1234 5678" not in text
    assert "XXXXX" in text
    assert "Customer Information" in text and "250.00" in text
    artifacts = Path("tmp/redaction-validation/text_replacement")
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "input.pdf").write_bytes(source.read_bytes())
    (artifacts / "redacted.pdf").write_bytes(output.read_bytes())
    (artifacts / "masks.yaml").write_bytes(plan.read_bytes())
    image.thumbnail((700, 1000))
    image.save(artifacts / "preview.png")
