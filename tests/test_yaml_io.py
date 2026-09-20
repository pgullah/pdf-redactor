from pathlib import Path

import pytest

from app.yaml_io import read_yaml, yaml_bytes
from main import main


def test_yaml_comments_unicode_and_round_trip(tmp_path):
    config = tmp_path / "rules.yaml"
    config.write_text(
        "# Supplier-specific labels\npurchase-order:\n  fields:\n"
        '    address:\n      labels: ["Adresse de livraison", "地址"]\n'
        "      max_lines: 4\n",
        encoding="utf-8",
    )
    data = read_yaml(config)
    assert data["purchase-order"]["fields"]["address"]["labels"][1] == "地址"
    config.write_bytes(yaml_bytes(data))
    assert read_yaml(config) == data
    assert config.read_text(encoding="utf-8").startswith("purchase-order:\n")


@pytest.mark.parametrize(
    "content",
    [
        "purchase-order: [unterminated",
        "purchase-order: {}\npurchase-order: {}\n",
        "purchase-order:\n  fields:\n    address: {}\n    address: {}\n",
        '!!python/object/apply:os.system ["echo forbidden"]',
        "? [a, b]\n: value\n",
    ],
)
def test_invalid_or_unsafe_yaml_rejected_without_output(tmp_path, capsys, content):
    config = tmp_path / "bad.yaml"
    config.write_text(content)
    output = tmp_path / "masks.yaml"
    result = main(
        [
            "plan",
            str(tmp_path / "input.pdf"),
            "-o",
            str(output),
            "--plugin",
            "purchase-order",
            "--plugin-config",
            str(config),
        ]
    )
    assert result == 1
    assert "Invalid YAML" in capsys.readouterr().err
    assert not output.exists()


@pytest.mark.parametrize(
    "content", ["", "- purchase-order\n", "purchase-order: null\n"]
)
def test_config_requires_mapping(tmp_path, capsys, content):
    config = tmp_path / "bad.yaml"
    config.write_text(content)
    assert (
        main(
            [
                "plan",
                str(tmp_path / "input.pdf"),
                "-o",
                str(tmp_path / "out.yaml"),
                "--plugin",
                "purchase-order",
                "--plugin-config",
                str(config),
            ]
        )
        == 1
    )
    assert "must be a mapping" in capsys.readouterr().err


def test_legacy_json_data_is_accepted(tmp_path):
    path = tmp_path / "legacy.json"
    path.write_text('{"version": 1, "masks": []}')
    assert read_yaml(path) == {"version": 1, "masks": []}


def test_bundled_configuration():
    config = read_yaml(Path(__file__).parents[1] / "examples" / "purchase-order.yaml")
    assert config["purchase-order"]["fields"]["buyer_name"]["max_lines"] == 1
