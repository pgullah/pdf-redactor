from pathlib import Path

import pytest
from test_plugins import document, line, text
from test_redaction import sample_pdf

from app.redaction.engine import load_detector, run_detector
from app.redaction.general import detect
from app.yaml_io import read_yaml
from main import main

CONFIG = Path(__file__).parents[1] / "examples" / "general.yaml"


def test_general_sample_fields_and_email():
    doc = document(
        [
            ("Name: Jane Smith", 0.1, 0.1),
            ("Address:", 0.1, 0.2),
            ("123 Green Road", 0.1, 0.23),
            ("London AB1 2CD", 0.1, 0.26),
            ("Date: 2026-09-20", 0.1, 0.29),
            ("Phone: 020 1234 5678", 0.1, 0.4),
            ("Email: jane@example.com", 0.1, 0.5),
            ("Total: 100.00", 0.1, 0.6),
        ]
    )
    config = read_yaml(CONFIG)["general"]
    assert text(detect(doc, config)) == {
        "person_name": "Jane Smith",
        "postal_address": "123 Green Road London AB1 2CD",
        "phone_number": "020 1234 5678",
        "email_address": "jane@example.com",
    }
    assert load_detector("general") is detect


def test_literals_regex_case_and_dedup():
    doc = document([("Jane Smith jane smith REF-123", 0.1, 0.1)])
    config = {
        "literals": {"name": {"values": ["Jane Smith"], "case_sensitive": True}},
        "patterns": {"reference": {"regex": r"REF-\d+"}},
    }
    masks, audit = run_detector("general", detect, doc, config, 3)
    assert len(masks) == 3
    assert {finding["field"] for finding in audit} == {"name", "reference"}
    config["literals"]["name"]["case_sensitive"] = False
    assert len(run_detector("general", detect, doc, config, 3)[0]) == 5


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"paterns": {}},
        {"fields": []},
        {"patterns": {"bad": {"regex": "["}}},
        {"patterns": {"bad": {"regex": "x", "case_sensitive": "false"}}},
        {"literals": {"bad": {"values": "Jane"}}},
        {"fields": {"name": {"labels": ["Name"], "max_line": 1}}},
    ],
)
def test_invalid_rules_fail_closed(config):
    with pytest.raises(ValueError, match="Detector general failed"):
        run_detector("general", detect, document([]), config, 3)


def test_general_cli_plan_apply(tmp_path, monkeypatch):
    source = tmp_path / "input.pdf"
    source.write_bytes(sample_pdf())
    monkeypatch.setattr(
        "app.masker.Store.ocr", lambda *args: line("Name: Jane Smith", 0.1, 0.1, 1)
    )
    plan = tmp_path / "masks.yaml"
    assert (
        main(
            [
                "plan",
                str(source),
                "-o",
                str(plan),
                "--plugin",
                "general",
                "--plugin-config",
                str(CONFIG),
            ]
        )
        == 0
    )
    assert {f["field"] for f in read_yaml(plan)["findings"]} == {"person_name"}
    assert (
        main(
            [
                "apply",
                str(source),
                "--masks",
                str(plan),
                "-o",
                str(tmp_path / "redacted.pdf"),
            ]
        )
        == 0
    )
