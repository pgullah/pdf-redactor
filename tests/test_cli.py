import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from pypdf import PdfReader
from test_redaction import sample_pdf

from main import main


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "input.pdf"
    path.write_bytes(sample_pdf())
    return path


def fake_ocr(monkeypatch):
    monkeypatch.setattr(
        "app.masker.Store.ocr",
        lambda *args: [
            {"text": "SECRET", "x": 0.1, "y": 0.2, "w": 0.2, "h": 0.1},
            {"text": "ACCOUNT", "x": 0.31, "y": 0.2, "w": 0.2, "h": 0.1},
        ],
    )


def test_plan_apply_and_source_binding(source, tmp_path, monkeypatch):
    fake_ocr(monkeypatch)
    plan = tmp_path / "masks.yaml"
    output = tmp_path / "redacted.pdf"
    assert main(["plan", str(source), "--text", "secret account", "-o", str(plan)]) == 0
    manifest = yaml.safe_load(plan.read_text())
    assert manifest["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert len(manifest["masks"]) == 6
    # Applying a reviewed plan must not invoke Tesseract.
    monkeypatch.setattr(
        "app.masker.Store.ocr", lambda *args: pytest.fail("Unexpected OCR")
    )
    assert main(["apply", str(source), "--masks", str(plan), "-o", str(output)]) == 0
    assert all(page.extract_text() == "" for page in PdfReader(output).pages)
    manifest["source_sha256"] = "wrong"
    plan.write_text(yaml.safe_dump(manifest))
    output.unlink()
    assert main(["apply", str(source), "--masks", str(plan), "-o", str(output)]) == 1
    assert not output.exists()


def test_no_match_and_no_clobber(source, tmp_path, monkeypatch):
    fake_ocr(monkeypatch)
    output = tmp_path / "out.pdf"
    assert main(["redact", str(source), "--text", "missing", "-o", str(output)]) == 3
    assert not output.exists()
    output.write_bytes(b"keep me")
    assert main(["redact", str(source), "--text", "SECRET", "-o", str(output)]) == 1
    assert output.read_bytes() == b"keep me"
    before = source.read_bytes()
    assert (
        main(["redact", str(source), "--text", "SECRET", "-o", str(source), "--force"])
        == 1
    )
    assert source.read_bytes() == before


def test_invalid_plan_rectangles(source, tmp_path, monkeypatch):
    fake_ocr(monkeypatch)
    plan = tmp_path / "plan.yaml"
    assert main(["plan", str(source), "--text", "SECRET", "-o", str(plan)]) == 0
    data = yaml.safe_load(plan.read_text())
    data["masks"][0]["x"] = -0.5
    plan.write_text(yaml.safe_dump(data))
    assert (
        main(
            [
                "apply",
                str(source),
                "--masks",
                str(plan),
                "-o",
                str(tmp_path / "out.pdf"),
            ]
        )
        == 1
    )


def test_terms_regex_and_ocr_yaml(source, tmp_path, monkeypatch):
    fake_ocr(monkeypatch)
    terms = tmp_path / "terms.txt"
    terms.write_text("SECRET\n\n")
    plan = tmp_path / "plan.yaml"
    assert (
        main(
            [
                "plan",
                str(source),
                "--terms-file",
                str(terms),
                "--regex",
                "ACCO.*",
                "-o",
                str(plan),
            ]
        )
        == 0
    )
    assert len(yaml.safe_load(plan.read_text())["masks"]) == 6
    ocr = tmp_path / "ocr.yaml"
    assert main(["ocr", str(source), "-o", str(ocr)]) == 0
    assert yaml.safe_load(ocr.read_text())["pages"][0]["words"][0]["text"] == "SECRET"


@pytest.mark.skipif(not shutil.which("tesseract"), reason="Tesseract not installed")
def test_real_cli_subprocess(source, tmp_path):
    output = tmp_path / "redacted.pdf"
    result = subprocess.run(
        [
            sys.executable,
            "main.py",
            "redact",
            str(source),
            "--text",
            "SECRET",
            "-o",
            str(output),
        ],
        cwd=Path(__file__).parents[1],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert output.is_file()
    assert len(PdfReader(output).pages) == 3
