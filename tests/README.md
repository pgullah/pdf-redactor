# Redaction validation

Run the real-document regression suite:

```sh
uv sync
uv run pytest tests/test_document_regression.py -v
```

Or select it by marker:

```sh
uv run pytest -m document_regression -v
```

Requires Tesseract and English language data. This suite fails with an installation message if Tesseract is unavailable; it does not report a skipped OCR test as successful validation. `TESSERACT_CMD` is supported.

## Document coverage

| Case | Input | Detector / workflow |
| --- | --- | --- |
| `purchase_order_digital` | Two-column purchase order with addresses and inline buyer | Bundled PO YAML |
| `purchase_order_scanned` | Image-only grayscale, mildly blurred version | Bundled PO YAML |
| `purchase_order_shifted` | Different page dimensions and shifted content | Same PO YAML; no fixed region rules |
| `general_digital` | Customer form with name, address, phone, account number, email | Bundled general YAML |
| `general_scanned` | Image-only grayscale, mildly blurred customer form | Bundled general YAML |
| `original_mixed` | Earlier sample: digital, scanned, rotated pages; source attachment and metadata | Literal SECRET rule on pages 1–2; explicit reviewed whole-page mask on sideways page 3 |
| `public_only` | Public catalog with no sensitive fields | Must return code 3 and create no plan/PDF |

Fixtures are generated deterministically in code from synthetic data; no private documents or downloads are needed. Each case invokes the actual CLI in a subprocess. PDF bytes need not be identical between runs because PDF timestamps/IDs can differ; text and geometry are fixed.

## What is checked

For purchase-order and general-form cases:

1. Required semantic fields appear in the mask plan.
2. Source OCR positively recognizes each expected sensitive value. This prevents a failure to read the original from masquerading as successful removal.
3. Expected sensitive glyph pixels are covered by masks. Expected regions come from fixture drawing coordinates, **not** detector output.
4. Sensitive values no longer appear when OCR reads the exported page image.
5. Public regions, including labels, dates, totals and item headings, are pixel-identical to the input. Their text remains readable. If full-page OCR skips an intact label beside a mask, a padded label crop is read separately.
6. Every mask pixel in the exported image is black, and every pixel outside all masks is unchanged.
7. Page count and displayed dimensions are preserved. Outputs contain no extractable text, attachments, form tree, page annotations, or private source metadata.

The mixed sample validates the same export integrity plus OCR removal on its two upright pages and full black pixels on the manually masked rotated page. It does **not** assert automatic recognition of sideways text. The negative sample verifies both `plan` and `redact` produce no output when there are no matches.

## Inspect artifacts

Each run writes to the ignored directory `tmp/redaction-validation/<case>/`:

- `input.pdf`: generated source.
- `masks.yaml`: actual CLI plan (except the negative case).
- `redacted.pdf`: actual exported file (except the negative case).
- `page-N-before.png` / `page-N-after.png`: full-resolution input/output page images.
- `page-N-export-preview.png`: a fresh PDFium rendering of the output PDF for visual review.
- `result.yaml`: pass/fail status and expected synthetic values/checks. Assertion details are in pytest output; a failed case remains marked failed.

The test-owned files at those paths are regenerated on each run. Do not put real documents there. Run different concurrent test sessions from separate checkouts to avoid sharing artifact paths.

## Extend the corpus

`redaction_samples.py` defines the PO/form fixtures, expected sensitive regions, expected preserved regions, detector flags, and expected semantic fields. Add a new case there and to the parameter list in `test_document_regression.py`. Mark each drawn value as sensitive or preserved before running OCR; never derive expected regions from the detector being tested.

Run all tests (unit, plugin, YAML, CLI, and document regression):

```sh
uv run pytest -q
```

These are controlled regression fixtures, not proof of complete redaction on arbitrary PDFs. They do not cover every font, language, handwriting style, scan defect, or supplier layout. Add representative synthetic examples of your own layouts when changing rules.

## Replacement text and colors

```sh
uv run pytest tests/test_mask_styles.py -v
```

These checks cover opaque replacement pixels independent of original content, exact clipping, black defaults, custom colors, invalid styles/fonts, per-field precedence and line grouping, region-only runs without OCR, black priority on overlaps, and a real OCR → styled YAML plan → PDF export. A generated example is saved under `tmp/redaction-validation/text_replacement/`.

## Same-line fields

```sh
uv run pytest tests/test_inline_fields.py -v
```

Tests cover multiple sensitive values on one line, public-field boundaries, longest labels, repeated fields, separators, label-like words inside values, PO fields, address columns, and ambiguous joined OCR tokens. Four real PDF cases (digital/scanned × black/text masking) verify sensitive glyph coverage and pixel-identical preservation of neighboring labels and public values. Outputs are saved under `tmp/redaction-validation/inline-*/`.

## Joined OCR words

```sh
uv run pytest tests/test_joined_ocr.py -v
```

These tests exercise tokens such as `Name:Jane;Phone:12345`, including real Tesseract output that contains multiple fields in one word. They check exact character-span selection, limited padding, rejection of forged geometry, coverage of expected sensitive glyphs, unchanged public glyphs, and exported black/text replacements. Artifacts are saved under `tmp/redaction-validation/joined-token-*/`.
