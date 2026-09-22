# Document-specific redaction plugins

The pipeline separates **recognition**, **field detection**, and **pixel removal**:

```text
PDF → OCR Document → detector(s) → named Findings → masks → new PDF
```

A detector decides what a value means; the common engine maps the selected OCR words to pixels. You do not need to provide fixed rectangles or know the actual names and addresses in advance. The same detector receives every page, so it can also inspect vendor identifiers, document headings, repeated fields, and other context.

## General masking configuration

[general.yaml](general.yaml) is a ready-to-edit configuration for arbitrary document types:

```sh
uv run python main.py plan document.pdf -o masks.yaml --plugin-config examples/general.yaml
```

The top-level key must be `general`. Supported sections:

| Section | Rule structure | Behavior |
| --- | --- | --- |
| `fields` | `labels: [Name, Full Name]`, `max_lines: 1` | Masks the value beside/below a label; labels remain visible. |
| `patterns` | `regex: 'pattern'`, `case_sensitive: false` | Matches OCR text anywhere on each page. |
| `literals` | `values: [Jane Smith]`, `case_sensitive: false` | Matches specified text, treating regex characters literally. |
| `stop_labels` | List of section headings | Stops multiline field extraction at these headings. |

Example additions inside `general`:

```yaml
literals:
  known_names:
    values: [Jane Smith, Alex Jones]
    case_sensitive: false
patterns:
  customer_reference:
    regex: '\bCUST-\d{6}\b'
    case_sensitive: true
```

Remove a rule to disable it. At least one rule is required; empty config, invalid expressions, and unrecognized rule options fail before export. Rules are additive and masks are deduplicated. Regex/literal rules mask whole words touched by a match; they do not cross page boundaries. Literal whitespace is normalized. Regexes use Python syntax and run without a separate timeout.

The supplied phone rule is label-based to avoid confusing arbitrary digit sequences with phone numbers. Names and addresses also rely on configured labels; detecting unlabeled values requires known literals or a custom entity detector. Review each document's results, especially multiline address boundaries. The general detector has no purchase-order-specific assumptions.

## Multiple fields on the same line

Both general and purchase-order field detectors split known inline labels before selecting values. No layout flag is needed. For example:

```text
Name: Jane Smith Phone: 020 1234 Date: 2026-09-21
```

With `Name` and `Phone` in `fields`, and `Date` in `stop_labels`, only `Jane Smith` and `020 1234` are masked. A selected field can appear anywhere on the line, including after a public field. The same boundaries apply to black masks and replacement text.

Use [inline-fields.yaml](inline-fields.yaml):

```sh
uv run python main.py plan input.pdf --config examples/inline-fields.yaml -o masks.yaml
```

The parser uses configured labels, preferring longer labels such as `Full Name` over `Name`. A label inside a line must have a colon, or follow an explicit `|` or `;` separator. This avoids treating a word such as `Date` inside `Mary Date Jones` as a new field. Ordinary labels at the start of a line or a separate visual column retain colon-optional behavior.

Add neighboring fields you want to preserve to `stop_labels`. Unknown labels, or dense same-line layouts without colons/separators, are not reliably inferred; use a custom detector or reviewed regions for those layouts. If OCR merges a value and the next known label into one word, field detection uses measured character boxes to select only the value. A label attached directly to its value (such as `Name:Jane`) is handled the same way. Mask padding is limited at neighboring characters so labels stay visible. If the separate character-box pass cannot be aligned, the pipeline retries using hOCR, which groups character boxes under their recognized words. Recovery requires matching text and page coordinates. Character positions must align exactly with the recognized word; if they do not, the pipeline requests a clearer scan or an explicit reviewed region instead of guessing. No new YAML option is needed.

Address continuation lines are restricted to the field's column, including when OCR combines adjacent columns into one line. Review the output because OCR can still miss or misread labels.

## Purchase orders

```sh
uv run python main.py plan order.pdf -o masks.yaml --plugin purchase-order
uv run python main.py apply order.pdf --masks masks.yaml -o redacted.pdf
```

Or run directly with a custom field configuration:

```sh
uv run python main.py redact order.pdf -o redacted.pdf --plugin purchase-order --plugin-config examples/purchase-order.yaml
```

The built-in detector recognizes these fields by default:

| Finding | Label examples | Value selected |
| --- | --- | --- |
| `ship_to` | Ship To, Deliver To, Delivery Address | Up to five value lines, including recipient and address |
| `bill_to` | Bill To, Billing Address | Up to five value lines, including name and address |
| `buyer_name` | Buyer, Buyer Name, Ordered By, Contact Name | One value line |
| `supplier_name` | Supplier Name, Vendor Name | One value line |

It retains field labels. Values can occur after a label or on nearby lines below it. Adjacent column headings, vertical gaps, section labels, and the configured line limit bound address blocks. `fields` replaces the default field selection. Each rule accepts `labels` and `max_lines` (1–20). An optional `stop_labels` list replaces the default section labels; configured and standard field labels remain boundaries.

This is a **label-and-layout heuristic**, not a universal purchase-order parser or a trained name/address recognizer. Unlabeled fields, missed OCR labels, unusual column layouts, long addresses, and missing section boundaries may produce incomplete or excessive selections. Validate each supplier's template and review plans/exports. There is no automatic guarantee that every address or person was detected. Use a custom detector when a layout needs more specific logic.

## Write your own plugin

A plugin exports a callable:

```python
from app.redaction import Document, Finding


def detect(document: Document, config: dict):
    if "purchase order" not in document.text.casefold():
        raise ValueError("This detector expects a purchase order")

    for page in document.pages:
        for line in page.lines:
            prefix = "requested by:"
            if line.text.casefold().startswith(prefix):
                words = line.words_for_span(len(prefix), len(line.text))
                yield Finding("requester_name", words)
```

Save it as `my_rules.py`, then run:

```sh
uv run python main.py plan order.pdf -o masks.yaml --plugin my_rules.py:detect
```

Both `path/to/file.py:function` and `importable.module:function` work. File plugins can import installed packages and project modules; use an importable Python package for plugins with sibling modules or relative imports. See [vendor_po.py](vendor_po.py) for a working example that combines custom field rules with email detection.

### Detector API

- `Document`: immutable `name`, `sha256`, `pages`, and combined `text`.
- `Page`: `index`, rendered pixel `width`/`height`, `words`, `lines`, and `text`.
- `Word`: page/index reference, text, OCR confidence, normalized `x`, `y`, `w`, `h`, and optional measured `characters`.
- `WordSlice`: a character range backed by an original word, with geometry calculated from its character boxes. Labeled-field detection uses these for joined tokens.
- `Line`: words, joined text, geometry helpers, and `words_for_span(start, end, precise=False)`. Set `precise=True` for character-aware spans when measured boxes are available.
- `Page.find(pattern, field="text", flags=re.IGNORECASE)`: yields findings from Python regex matches, mapped to whole OCR words.
- `Finding(field, words, reason="")`: semantic field name and original OCR `Word` objects to redact. `reason` is available in memory but is not saved in plans.

`Line.words_for_span` offsets refer to `line.text`, where words are separated by one space. `Page.find` uses `page.text` in OCR reading order. `page.lines` uses Tesseract line identities, separates large horizontal gaps, and sorts visually; complex reading order may still need detector-specific handling.

The detector must return/yield an iterable of `Finding` objects. Findings may reference original words or validated `WordSlice` ranges backed by those words. An empty iterable means no findings. Return original word objects or slices of them; manufactured coordinates or references to another document are rejected. The engine applies `--padding`, deduplicates masks, validates them, and reuses the existing permanent image-only export. No confidence threshold silently discards words.

For NER, run a model over `line.text` or `page.text`, map predicted character spans back to the corresponding words, and yield a `Finding` with a stable entity label. No NER model, LLM, or external service is included or invoked by default.

### Configuration and composition

The config file is a YAML mapping keyed by the **exact plugin identifier** supplied on the command line. An optional top-level `masking` section configures appearance and explicit regions (see [mask appearance](MASKING.md)):

```yaml
"examples/vendor_po.py:detect":
  recipient_labels:
    - Deliver To
    - Consignee
```

```sh
uv run python main.py plan order.pdf -o masks.yaml --plugin examples/vendor_po.py:detect --plugin-config vendor.yaml --text "Internal reference"
```

YAML supports comments and multiline configuration. Files are parsed with a safe loader; Python object tags and duplicate mapping keys are rejected. Quote values such as `"yes"`, `"no"`, or dates when you intend strings. Existing JSON config/plan files remain readable because their syntax is valid YAML; newly generated plans and OCR output use YAML.

Repeat `--plugin` to combine detectors. `--text`, `--regex`, and `--terms-file` continue to work and use the same finding-to-mask engine. Detectors are additive: all findings are merged; one plugin cannot undo another plugin's selections. When no plugin is specified, a general rules section selects the default general detector. Explicit --plugin selections replace the default; automatic document-type routing belongs in your own plugin.

### Review, errors, and trust

Black-only plans use version 1; styled plans use version 2. Both are supported and include an optional `findings` audit containing detector identifiers, semantic field labels, and page/word indexes. It excludes OCR values and free-form reasons. Keep field labels generic; a plugin should not put sensitive values in them. The `masks` array is authoritative when applying a plan. If you edit masks manually, the original findings audit is not recomputed.

The `apply` command needs neither the plugin nor OCR; it verifies the source fingerprint and applies the reviewed masks. No findings returns exit code 3 without writing output. Invalid findings, plugin exceptions (including failures after partial results), or invalid configuration return an error without writing output.

Plugins are **trusted Python code**, not sandboxed expressions. They run with your process permissions and may read files, use the network, or execute other code. Load only plugins you trust. The built-in detector and supplied example perform no network calls.
