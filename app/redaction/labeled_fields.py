"""Shared label-and-layout field extraction for document detectors."""

import re

from .model import Finding


def prefix(text, labels):
    for label in sorted(labels, key=len, reverse=True):
        match = re.match(
            r"^\s*" + re.escape(label) + r"(?=\s|:|$)\s*:?\s*", text, re.IGNORECASE
        )
        if match:
            return match.end()
    return None


def detect_fields(document, fields, stops):
    """Select values after configured labels, using nearby lines for address blocks."""
    if not isinstance(fields, dict) or not fields:
        raise ValueError("Field rules must be a non-empty mapping.")
    for name, rule in fields.items():
        if not isinstance(name, str) or not isinstance(rule, dict):
            raise TypeError("Each field must have a name and a rule object.")
        labels = rule.get("labels")
        if (
            not isinstance(labels, list)
            or not labels
            or not all(isinstance(s, str) and s.strip() for s in labels)
        ):
            raise ValueError(f"Field {name} needs non-empty string labels.")
        if (
            type(rule.get("max_lines", 1)) is not int
            or not 1 <= rule.get("max_lines", 1) <= 20
        ):
            raise ValueError("max_lines must be between 1 and 20.")
    if not isinstance(stops, list) or not all(isinstance(s, str) and s for s in stops):
        raise ValueError("stop_labels must be a list of non-empty strings.")
    boundaries = stops + [label for rule in fields.values() for label in rule["labels"]]
    for page in document.pages:
        for anchor in page.lines:
            for name, rule in fields.items():
                end = prefix(anchor.text, rule["labels"])
                if end is None:
                    continue
                # Keep the label, mask only its value. Nearby headings define column bounds.
                right = min(
                    (
                        line.x
                        for line in page.lines
                        if line.x > anchor.x + 0.08
                        and abs(line.y - anchor.y) < 0.025
                        and prefix(line.text, boundaries) is not None
                    ),
                    default=1,
                )
                value = list(anchor.words_for_span(end, len(anchor.text)))
                selected_lines = 1 if value else 0
                previous_bottom = anchor.bottom
                line_height = max(word.h for word in anchor.words)
                for line in page.lines:
                    if line is anchor or line.y < anchor.y - 0.005:
                        continue
                    if not (anchor.x - 0.025 <= line.x < right - 0.01):
                        continue
                    if selected_lines >= rule.get("max_lines", 1):
                        break
                    if line.y - previous_bottom > max(0.025, line_height * 1.8):
                        break
                    if prefix(line.text, boundaries) is not None:
                        break
                    value.extend(
                        word for word in line.words if word.x + word.w <= right + 0.005
                    )
                    selected_lines += 1
                    previous_bottom = line.bottom
                if value:
                    yield Finding(
                        name, tuple(value), "Value following a configured field label"
                    )
