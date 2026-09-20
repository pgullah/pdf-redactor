"""Config-driven label, literal, and regex rules for any document type."""

import re

from .labeled_fields import detect_fields


def detect(document, config):
    allowed = {"fields", "stop_labels", "patterns", "literals"}
    if set(config) - allowed:
        raise ValueError(
            "Unknown general config keys; use fields, stop_labels, patterns, literals."
        )
    fields = config.get("fields", {})
    patterns = config.get("patterns", {})
    literals = config.get("literals", {})
    if not all(isinstance(group, dict) for group in (fields, patterns, literals)):
        raise ValueError("fields, patterns, and literals must be mappings.")
    if not any((fields, patterns, literals)):
        raise ValueError(
            "General detector requires at least one field, pattern, or literal rule."
        )
    stops = config.get("stop_labels", [])
    if not isinstance(stops, list) or not all(isinstance(s, str) and s for s in stops):
        raise ValueError("stop_labels must be a list of non-empty strings.")
    for rule in fields.values():
        if not isinstance(rule, dict) or set(rule) - {"labels", "max_lines"}:
            raise ValueError("Field rules accept labels and max_lines only.")
    compiled = []
    for kind, group in (("patterns", patterns), ("literals", literals)):
        for name, rule in group.items():
            if (
                not isinstance(name, str)
                or not name.strip()
                or not isinstance(rule, dict)
            ):
                raise ValueError("Each rule needs a non-empty name and a mapping.")
            value_key = "regex" if kind == "patterns" else "values"
            if set(rule) - {value_key, "case_sensitive"}:
                raise ValueError(
                    f"Rule {name} accepts {value_key} and case_sensitive only."
                )
            case_sensitive = rule.get("case_sensitive", False)
            if type(case_sensitive) is not bool:
                raise ValueError("case_sensitive must be true or false.")
            flags = 0 if case_sensitive else re.IGNORECASE
            if kind == "patterns":
                expression = rule.get("regex")
                if not isinstance(expression, str) or not expression.strip():
                    raise ValueError(f"Rule {name} needs a non-empty regex string.")
                compiled.append((name, re.compile(expression, flags)))
            else:
                values = rule.get("values")
                if (
                    not isinstance(values, list)
                    or not values
                    or not all(isinstance(v, str) and v.strip() for v in values)
                ):
                    raise ValueError(
                        f"Rule {name} needs a non-empty list of literal values."
                    )
                compiled.extend(
                    (name, re.compile(re.escape(" ".join(value.split())), flags))
                    for value in values
                )
    if fields:
        yield from detect_fields(document, fields, stops)
    for page in document.pages:
        for field, pattern in compiled:
            yield from page.find(pattern.pattern, field=field, flags=pattern.flags)
