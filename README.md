# PDF Masker

A local Python application for permanently masking content in PDFs. It uses **pytesseract**, the Python wrapper for Tesseract, with a command-line pipeline. Scanned and digitally generated PDFs follow the same image-based pipeline.

## Run

Requires Python 3.12+ and the Tesseract executable. Python packages alone do not install the OCR engine.

### Install Tesseract

- **macOS:** `brew install tesseract`
- **Ubuntu / Debian:** `sudo apt install tesseract-ocr`
- **Windows:** install a Windows build listed in the [Tesseract installation documentation](https://tesseract-ocr.github.io/tessdoc/Installation.html), then add its installation directory to PATH.

Check with `tesseract --version`. If the executable is elsewhere, set `TESSERACT_CMD` to its full path before starting the app. For example, in PowerShell:

```powershell
$env:TESSERACT_CMD = 'C:\Program Files\Tesseract-OCR\tesseract.exe'
```

### Install Python packages and start

With [uv](https://docs.astral.sh/uv/):

```sh
uv sync
uv run python main.py --help
```

## CLI pipeline

No browser or running server is needed. Commands run locally and clean up their temporary page images when finished.

### Redact in one command

```sh
uv run python main.py redact input.pdf -o redacted.pdf --text "Jane Smith" --text "123-45-6789"
```

Match literal terms from a UTF-8 file (one term per line), or Python regular expressions:

```sh
uv run python main.py redact input.pdf -o redacted.pdf --terms-file terms.txt
uv run python main.py redact input.pdf -o redacted.pdf --regex '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'
```

Matching is case-insensitive by default. Use `--case-sensitive` to change this. Literal whitespace is normalized to single spaces; matches use OCR reading order and do not cross pages. Every word touched by a match is masked in full. Regex patterns should be trusted: they run using Python's regular expression engine without a separate timeout.

### General masking configuration

`general` is the default plugin for `plan` and `redact`. Supply your YAML rules with `--config PATH`; `--plugin general` is optional. An explicit `--plugin` replaces the default (repeat it to combine detectors). Text/regex-only and region-only commands still work without a general rules section.


Use [examples/general.yaml](examples/general.yaml) for common document fields and patterns:

```sh
uv run python main.py plan input.pdf -o masks.yaml --plugin-config examples/general.yaml
uv run python main.py apply input.pdf --masks masks.yaml -o redacted.pdf
```

The example masks labeled names, postal/shipping/billing addresses, phone numbers, and account numbers, plus email patterns anywhere on a page. Edit the labels for your documents, add exact values under `literals`, or remove unwanted rules. Unlabeled names and addresses require known literal values or a custom detector; this is not universal entity recognition.

Known fields on the same line are handled automatically: values stop at the next configured label. Joined OCR tokens use measured character boxes to separate labels and values. Put sensitive labels in `fields` and neighboring public labels in `stop_labels`. See [inline-fields.yaml](examples/inline-fields.yaml) and [same-line detection details](examples/PLUGINS.md#multiple-fields-on-the-same-line).

### Document-specific fields and custom plugins

Mask values by their role in a document, without knowing each name or address in advance:

```sh
uv run python main.py plan order.pdf -o masks.yaml --plugin purchase-order
uv run python main.py apply order.pdf --masks masks.yaml -o redacted.pdf
```

The purchase-order detector locates shipping/billing name-and-address blocks and labeled buyer/supplier names using OCR text and layout. Customize its labels and fields with `--plugin-config examples/purchase-order.yaml`. It is a configurable heuristic; validate it against your suppliers' templates.

For your own behavior, implement `detect(document, config)` and return semantic `Finding` objects referencing OCR words:

```sh
uv run python main.py redact order.pdf -o redacted.pdf --plugin examples/vendor_po.py:detect
```

See **[the plugin guide](examples/PLUGINS.md)** for the API, configuration, entity-span mapping, and composition. Plugins can be combined with existing text/regex rules. Plugins are trusted Python code and are not sandboxed.

### Black masks, replacement text, and styled regions

Black remains the default. Set `masking.default`, `masking.fields`, or `masking.regions` in YAML to use an opaque fill or replacement text such as `XXXXX`:

```sh
uv run python main.py plan input.pdf -o masks.yaml --config examples/masking.yaml
uv run python main.py apply input.pdf --masks masks.yaml -o redacted.pdf
```

Regions alone do not need OCR:

```sh
uv run python main.py redact input.pdf -o redacted.pdf --config examples/regions.yaml
```

See [the mask appearance guide](examples/MASKING.md) for modes, field overrides, coordinates, overlap behavior, and fonts. Source pixels are erased before replacement text is drawn. Styled plans use version 2; old black-only plans remain supported.

### Review a plan before applying it

```sh
uv run python main.py plan input.pdf -o masks.yaml --text "Jane Smith"
# Review or edit masks.yaml, then:
uv run python main.py apply input.pdf --masks masks.yaml -o redacted.pdf
```

Configuration, plans, and OCR output use YAML. Existing JSON configuration and plan files remain readable. YAML comments are supported; duplicate keys and Python object tags are rejected.

The plan includes a SHA-256 fingerprint of the source PDF, rendering dimensions, and masks. `apply` validates the fingerprint and coordinates and runs **without OCR**. It refuses plans for a different source PDF or rendering geometry.

Each mask has this shape:

```yaml
page: 0
x: 0.1
y: 0.2
w: 0.3
h: 0.05
```

Page indexes start at **zero**. Coordinates start at the **top-left** and are normalized from 0 to 1 relative to the rendered page. You can edit, remove, or add rectangles in the plan while preserving its other fields. Plans contain no extracted text, but OCR YAML does.

### Extract OCR results

```sh
uv run python main.py ocr input.pdf -o words.yaml --language eng
```

This writes word text, confidence scores, bounding boxes, and page geometry. Use it to inspect recognition results or feed another program. The YAML contains unredacted text and should stay private.

### Options and exit codes

- `--language eng+fra`: use installed Tesseract language packs (default: `eng`).
- `--padding 3`: extend matched word masks by this many rendered pixels (default: 3; `plan`/`redact` only).
- `--force`: replace an existing output file. The input PDF and input terms/plan files can never be overwritten.
- `--help`: available at the top level and on every command.

Exit codes: **0** success, **1** processing or file error, **2** invalid command arguments, **3** no matching text (no output written), **130** interrupted. Progress and error messages go to stderr. Output is written to `-o` only after processing completes. Existing output directories must already exist. Empty plans are not exported.

Example shell automation:

```sh
uv run python main.py redact input.pdf -o redacted.pdf --terms-file terms.txt && echo "Redaction completed"
```

OCR can miss sensitive content. Review the exported PDF before sharing. This pipeline does not automatically decide which information is sensitive.

## How redaction works

```text
PDF → PDFium rendering → page PNGs → Tesseract word boxes
    → reviewed masks → pixel replacement → new image-only PDF
```

- `pypdfium2` renders every page at 200 DPI, including visible annotations and form appearances.
- `pytesseract` returns word text, confidence scores, and bounding boxes.
- The CLI uses normalized image coordinates for consistent mask placement. OCR word masks include a small margin.
- Pillow replaces pixels inside selected regions with solid black by default, or an opaque fill/replacement text patch when configured.
- ReportLab constructs a fresh PDF from the modified images, retaining rendered page dimensions. It does **not** import original PDF objects, text layers, attachments, metadata, or annotations. Images use lossless encoding.
- The input file is never modified. An export is a separate file.

## Privacy and limits

All processing runs locally in the CLI, without a server, account, analytics, or built-in cloud OCR calls. PDFium operations are serialized because the native library is not thread safe. Custom plugins are trusted Python code and may have their own external integrations.

Rendered, unmasked pages are stored in a private temporary workspace during each command; OCR results are held in memory. The workspace is removed when the command finishes, including ordinary errors and keyboard interruption. A forced process termination may leave a `pdf-masker-*` directory in your operating system's temporary folder. File deletion is ordinary filesystem deletion, not secure disk erasure.

Limits: 50 MB per input, 100 pages, 25 million rendered pixels per page, 250 million pixels per document. Large documents may reach the pixel limit before the page limit. Password-protected PDFs must be unlocked first. OCR has a 120-second per-page timeout.

OCR is imperfect, especially for handwriting, skewed scans, small text, unusual layouts, and unsupported languages. Search is literal over recognized words; it is not automatic personal-information detection. Manual review remains necessary. Exported PDFs are image-only, so selectable text, accessibility structure, links, forms, and digital signatures are not preserved. Page images can make output files larger and less sharp at high zoom.

## Validate sample documents

```sh
uv run pytest tests/test_document_regression.py -v
```

Seven real-OCR cases cover digital/scanned purchase orders, a shifted layout, digital/scanned general forms, the earlier mixed-page sample, and a public-only negative case. Tests check sensitive glyph coverage, preserved content, pixel replacement, and removal of source text/attachments. The rotated page uses an explicitly reviewed manual mask.

Inspect the generated inputs, redacted PDFs, plans, previews, and per-case results in `tmp/redaction-validation/`. See [the validation guide](tests/README.md) for checks, limitations, and how to add samples. Tesseract is required; these regression tests fail instead of silently skipping if it is missing.

## Development and validation

```sh
uv sync
uv run pytest -q
uv run ruff check main.py app examples tests
```

The tests exercise digital, scanned, and rotated pages; actual OCR when Tesseract is installed; mask pixel replacement; preservation of pixels outside masks; absence of source text, attachments and private metadata in exports; invalid masks; temporary workspace cleanup; and manual export without OCR. The real OCR test is skipped when Tesseract is absent.

Generated test PDFs and a rendered export preview go into ignored `tmp/pdfs/`.

- `main.py`: CLI argument parsing and pipeline orchestration
- `app/redaction/`: immutable OCR model, plugin loader, shared decision engine, and PO detector
- `examples/`: custom detector, PO field config, and plugin guide
- `app/masker.py`: PDF rendering, OCR, temporary workspaces, and redaction export
- `tests/`: CLI, plugin, and end-to-end pipeline checks

## Project structure

```text
main.py                 # CLI entry point and command orchestration
app/
  __init__.py
  masker.py             # PDF rendering, OCR, workspaces, and export
  yaml_io.py            # Safe YAML loading and serialization
  redaction/
    __init__.py         # Public detector types
    model.py            # OCR document, page, line, word, and finding models
    engine.py           # Plugin loading and finding-to-mask conversion
    labeled_fields.py   # Shared field extraction
    general.py          # Configurable general detector
    purchase_order.py   # Purchase-order detector
    rendering.py        # Mask styles, validation, and pixel replacement
examples/               # YAML configurations and custom plugin examples
tests/                  # Unit, CLI, and document regression tests
```

Custom Python plugins should import shared types from `app.redaction`, for example `from app.redaction import Finding`. Importable plugin identifiers now use `app.redaction.purchase_order:detect`; built-in identifiers (`general`, `purchase-order`) and file-based plugin arguments are unchanged.
