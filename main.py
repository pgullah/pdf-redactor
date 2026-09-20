"""Local, scriptable OCR and image-only PDF redaction."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import tempfile
from pathlib import Path

import pytesseract

from app.masker import DPI, MAX_BYTES, Store
from app.redaction import Document
from app.redaction.engine import load_detector, run_detector
from app.redaction.rendering import parse_masking, validate_mask
from app.yaml_io import read_yaml, yaml_bytes


def parser():
    root = argparse.ArgumentParser(
        description="OCR and permanently redact PDFs locally with Tesseract."
    )
    commands = root.add_subparsers(dest="command", required=True)
    for command, description in (
        ("ocr", "Extract words and coordinates as YAML"),
        ("plan", "Find text and write a reviewable mask plan"),
        ("apply", "Apply a saved mask plan without running OCR"),
        ("redact", "Run OCR, match text, and export a redacted PDF"),
    ):
        sub = commands.add_parser(command, help=description)
        sub.add_argument("input", type=Path, help="Source PDF")
        sub.add_argument(
            "-o",
            "--output",
            required=True,
            type=Path,
            help="Output YAML (ocr/plan) or PDF (apply/redact)",
        )
        sub.add_argument(
            "--force",
            action="store_true",
            help="Replace an existing output (never the input)",
        )
        if command != "apply":
            sub.add_argument(
                "--language",
                default="eng",
                help="Tesseract language, e.g. eng or eng+fra",
            )
        if command in ("plan", "redact"):
            sub.add_argument(
                "--plugin",
                action="append",
                default=[],
                help="Detector: general, purchase-order, module:function, or path.py:function; repeatable",
            )
            sub.add_argument(
                "--plugin-config",
                "--config",
                type=Path,
                help="YAML detector rules and optional masking styles/regions",
            )
            sub.add_argument(
                "--text",
                action="append",
                default=[],
                help="Literal text or phrase; repeat for multiple terms",
            )
            sub.add_argument(
                "--regex",
                action="append",
                default=[],
                help="Python regular expression; repeat for multiple patterns",
            )
            sub.add_argument(
                "--terms-file",
                type=Path,
                help="UTF-8 file containing one literal term per line",
            )
            sub.add_argument(
                "--case-sensitive",
                action="store_true",
                help="Match case (default: ignore case)",
            )
            sub.add_argument(
                "--padding",
                type=int,
                default=3,
                help="Extra pixels around matched words (default: 3)",
            )
        if command == "apply":
            sub.add_argument(
                "--masks",
                required=True,
                type=Path,
                help="YAML plan created by the plan command",
            )
    return root


def progress(message):
    print(message, file=sys.stderr)


def check_output(output, inputs, force):
    for source in inputs:
        if output.resolve() == source.resolve() or (
            output.exists() and source.exists() and os.path.samefile(output, source)
        ):
            raise ValueError("Output must not overwrite an input file.")
    if output.exists() and not force:
        raise ValueError(f"Output already exists: {output}. Use --force to replace it.")
    if not output.parent.is_dir():
        raise ValueError(f"Output directory does not exist: {output.parent}")


def write_output(path, content, force):
    """Stage output beside its destination; publish only a complete file."""
    fd, temporary = tempfile.mkstemp(prefix=".pdf-masker-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if force:
            os.replace(temporary, path)
        else:
            # Atomic create-if-absent; unlike replace, cannot clobber a racing writer.
            os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def patterns(args, allow_regions=False):
    terms = list(args.text)
    if args.terms_file:
        terms.extend(args.terms_file.read_text(encoding="utf-8").splitlines())
    terms = [" ".join(term.split()) for term in terms if term.strip()]
    flags = 0 if args.case_sensitive else re.IGNORECASE
    result = [re.compile(re.escape(term), flags) for term in terms]
    for pattern in args.regex:
        if not pattern:
            raise ValueError("Regex patterns must not be empty.")
        result.append(re.compile(pattern, flags))
    if not result and not args.plugin and not allow_regions:
        raise ValueError(
            "Provide --plugin, --text, --regex, --terms-file, or configured masking.regions."
        )
    if not 0 <= args.padding <= 100:
        raise ValueError("Padding must be between 0 and 100 pixels.")
    return result


def validate_plan(plan, digest, pages):
    if not isinstance(plan, dict) or plan.get("version") not in (1, 2):
        raise ValueError("Unsupported mask plan. Expected version 1 or 2.")
    if plan.get("source_sha256") != digest:
        raise ValueError("Mask plan does not belong to this PDF (SHA-256 mismatch).")
    if plan.get("dpi") != DPI or plan.get("pages") != pages:
        raise ValueError("Mask plan page geometry does not match this rendering.")
    masks = plan.get("masks")
    if not isinstance(masks, list) or not 1 <= len(masks) <= 20000:
        raise ValueError("Mask plan must contain between 1 and 20,000 masks.")
    for mask in masks:
        validate_mask(mask, len(pages))
    return masks


def run(args):
    inputs = [args.input]
    for option in ("terms_file", "masks", "plugin_config"):
        if getattr(args, option, None):
            inputs.append(getattr(args, option))
    for spec in getattr(args, "plugin", []):
        module = spec.rpartition(":")[0]
        if module.endswith(".py"):
            inputs.append(Path(module))
    check_output(args.output, inputs, args.force)
    config = {}
    if getattr(args, "plugin_config", None):
        config = read_yaml(args.plugin_config)
        if not isinstance(config, dict) or any(
            not isinstance(v, dict) for v in config.values()
        ):
            raise ValueError(
                "Plugin config must be a mapping keyed by plugin identifier, with mapping values."
            )
        if set(config) - set(args.plugin) - {"masking"}:
            raise ValueError(
                "Plugin config contains a key that was not supplied with --plugin."
            )
    masking = parse_masking(
        config.pop("masking", {}),
        args.plugin_config.parent
        if getattr(args, "plugin_config", None)
        else Path.cwd(),
    )
    expressions = (
        patterns(args, bool(masking["regions"]))
        if args.command in ("plan", "redact")
        else []
    )
    detectors = [(spec, load_detector(spec)) for spec in getattr(args, "plugin", [])]
    if args.command != "apply" and not re.fullmatch(
        r"[a-zA-Z0-9_]+(?:\+[a-zA-Z0-9_]+)*", args.language
    ):
        raise ValueError("Invalid language. Use language codes such as eng or eng+fra.")
    with args.input.open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    digest = hashlib.sha256(data).hexdigest()
    store = Store()
    try:
        progress("Rendering PDF…")
        doc = store.create(data, args.input.name)
        plan = {
            "version": 1,
            "source_sha256": digest,
            "dpi": DPI,
            "pages": doc.pages,
            "masks": [],
        }
        if args.command == "apply":
            plan = read_yaml(args.masks)
            masks = validate_plan(plan, digest, doc.pages)
        else:
            recognized = []
            masks = list(masking["regions"])
            for region in masks:
                validate_mask(region, len(doc.pages))
            for page in doc.pages:
                if args.command == "ocr" or expressions or detectors:
                    progress(f"OCR page {page['index'] + 1}/{len(doc.pages)}…")
                words = (
                    store.ocr(doc.id, page["index"], args.language)
                    if (args.command == "ocr" or expressions or detectors)
                    else []
                )
                recognized.append({**page, "words": words})

            if args.command == "ocr":
                content = {
                    "version": 1,
                    "source_sha256": digest,
                    "dpi": DPI,
                    "language": args.language,
                    "pages": recognized,
                }
                write_output(
                    args.output,
                    yaml_bytes(content),
                    args.force,
                )
                progress(f"Wrote OCR YAML: {args.output}")
                return 0
            document = Document.from_ocr(args.input.name, digest, recognized)
            if expressions:

                def text_detector(document, config):
                    for page in document.pages:
                        for expression in expressions:
                            yield from page.find(
                                expression.pattern, flags=expression.flags
                            )

                detectors.insert(0, ("text", text_detector))
            audit = []
            for name, detector in detectors:
                progress(f"Running detector: {name}…")
                found, decisions = run_detector(
                    name,
                    detector,
                    document,
                    config.get(name, {}),
                    args.padding,
                    masking,
                )
                masks.extend(found)
                audit.extend(decisions)
            # Regions come last so their text/fill style overrides prior text/fill;
            # the exporter always gives black masks priority in overlaps.
            masks = masks[len(masking["regions"]) :] + masking["regions"]
            unique = {}
            for mask in masks:
                unique[yaml_bytes(mask)] = mask
            masks = list(unique.values())
            plan["findings"] = audit
            if not masks:
                progress("No matching text found; no output written.")
                return 3
            if any("style" in mask for mask in masks):
                plan["version"] = 2
            plan["masks"] = masks
            validate_plan(plan, digest, doc.pages)
        if args.command == "plan":
            content = yaml_bytes(plan)
        else:
            progress(f"Applying {len(masks)} masks…")
            content = store.export(doc.id, masks)
        write_output(args.output, content, args.force)
        progress(
            f"Wrote {args.output} ({len(masks)} masks across {len(doc.pages)} pages)."
        )
        return 0
    finally:
        store.close()


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return run(args)
    except pytesseract.TesseractNotFoundError:
        progress(
            "Error: Tesseract is not installed or not on PATH. Set TESSERACT_CMD if needed."
        )
    except pytesseract.TesseractError:
        progress("Error: OCR failed. Check Tesseract and the selected language packs.")
    except (OSError, ValueError, RuntimeError, re.error) as exc:
        progress(f"Error: {exc}")
    except KeyboardInterrupt:
        progress("Cancelled.")
        return 130
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
