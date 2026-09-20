"""Safe YAML for CLI configuration, mask plans, and OCR output."""

from pathlib import Path

import yaml


class UniqueKeyLoader(yaml.SafeLoader):
    """Reject duplicate keys instead of silently replacing a redaction rule."""

    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        keys = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in keys
                keys.add(key)
            except TypeError as exc:
                raise yaml.constructor.ConstructorError(
                    None,
                    None,
                    "Mapping keys must be scalar values",
                    key_node.start_mark,
                ) from exc
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    None, None, "Duplicate mapping key", key_node.start_mark
                )
        return super().construct_mapping(node, deep=deep)


def read_yaml(path: Path):
    try:
        # SafeLoader accepts only data: Python object tags cannot execute code.
        return yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        problem = getattr(exc, "problem", None) or "invalid YAML data"
        raise ValueError(f"Invalid YAML in {path}{location}: {problem}") from exc


def yaml_bytes(value) -> bytes:
    return yaml.safe_dump(value, sort_keys=False, allow_unicode=True).encode("utf-8")
