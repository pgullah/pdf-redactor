"""Detector loading and conversion of semantic findings to validated masks."""

import importlib
import importlib.util
import sys
from pathlib import Path
from uuid import uuid4

from .model import Finding, WordSlice
from .rendering import BLACK


def load_detector(spec):
    if spec == "general":
        from .general import detect

        return detect
    if spec == "purchase-order":
        from .purchase_order import detect

        return detect
    module_name, separator, function = spec.rpartition(":")
    if not separator or not module_name or not function:
        raise ValueError(
            "Plugin must be general, purchase-order, module:function, or /path/plugin.py:function."
        )
    try:
        if module_name.endswith(".py"):
            name = f"_pdf_masker_plugin_{uuid4().hex}"
            module_spec = importlib.util.spec_from_file_location(
                name, Path(module_name)
            )
            if module_spec is None or module_spec.loader is None:
                raise ValueError("Cannot load plugin file.")
            module = importlib.util.module_from_spec(module_spec)
            sys.modules[name] = module
            try:
                module_spec.loader.exec_module(module)
            except BaseException:
                sys.modules.pop(name, None)
                raise
        else:
            module = importlib.import_module(module_name)
        detector = getattr(module, function)
        if not callable(detector):
            raise TypeError("Plugin entry point must be callable.")
        return detector
    except Exception as exc:
        raise ValueError(f"Cannot load plugin {spec}: {exc}") from exc


def run_detector(name, detector, document, config, padding, masking=None):
    """Validate word ownership and deduplicate masks. Never trust plugin geometry."""
    known = {
        (word.page, word.index): word for page in document.pages for word in page.words
    }
    pages = {page.index: page for page in document.pages}
    masks, audit, seen = [], [], set()
    masking = masking or {"default": BLACK, "fields": {}}
    try:
        for index, finding in enumerate(detector(document, config)):
            if index >= 20000:
                raise ValueError("Detector exceeded 20,000 findings.")
            if (
                not isinstance(finding, Finding)
                or not isinstance(finding.field, str)
                or not finding.field
            ):
                raise ValueError(
                    "Detectors must yield Finding objects with a non-empty field name."
                )
            style = masking["fields"].get(
                f"{name}.{finding.field}",
                masking["fields"].get(finding.field, masking["default"]),
            )
            refs, selected = [], []
            for word in finding.words:
                key = (word.page, word.index)
                source_word = word.word if isinstance(word, WordSlice) else word
                if known.get(key) != source_word:
                    raise ValueError(
                        "Finding references a word outside the OCR document."
                    )
                ref = {"page": word.page, "word": word.index}
                if isinstance(word, WordSlice):
                    ref.update(start=word.start, end=word.end)
                refs.append(ref)
                selected.append(word)
            if style["mode"] == "text":
                # One replacement per contiguous selected line span. Do not bridge
                # unselected words, page boundaries, or separate visual columns.
                groups = []
                selected_parts = {}
                for word in selected:
                    selected_parts.setdefault((word.page, word.index), []).append(word)
                for page in document.pages:
                    for line in page.lines:
                        group = []
                        for word in line.words:
                            if (word.page, word.index) in selected_parts:
                                group.extend(selected_parts[(word.page, word.index)])
                            elif group:
                                groups.append(group)
                                group = []
                        if group:
                            groups.append(group)
            else:
                groups = [[word] for word in selected]
            for group in groups:
                page = pages[group[0].page]
                px, py = padding / page.width, padding / page.height
                left_limit = max(
                    (word.left_limit for word in group if isinstance(word, WordSlice)),
                    default=0,
                )
                right_limit = min(
                    (word.right_limit for word in group if isinstance(word, WordSlice)),
                    default=1,
                )
                x = max(left_limit, min(word.x for word in group) - px)
                y = max(0, min(word.y for word in group) - py)
                mask = {
                    "page": page.index,
                    "x": x,
                    "y": y,
                    "w": min(right_limit, max(word.x + word.w for word in group) + px)
                    - x,
                    "h": min(1, max(word.y + word.h for word in group) + py) - y,
                }
                if style != BLACK:
                    mask["style"] = style
                key = (
                    tuple(mask[k] for k in ("page", "x", "y", "w", "h")),
                    tuple(style.items()),
                )
                if key not in seen:
                    masks.append(mask)
                    seen.add(key)
            if refs:
                # No recognized text or free-form reasons in the persisted audit.
                audit.append({"detector": name, "field": finding.field, "words": refs})
    except Exception as exc:
        raise ValueError(f"Detector {name} failed: {exc}") from exc
    return masks, audit
