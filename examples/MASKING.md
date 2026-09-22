# Mask appearance and field/region overrides

Solid black remains the default. Configure appearance separately from detection rules in the same YAML file:

```yaml
masking:
  default:
    mode: black
  fields:
    person_name:
      mode: text
      text: XXXXX
      background: '#FFFFFF'
      foreground: '#000000'
    postal_address:
      mode: text
      text: '[REMOVED]'
    account_number:
      mode: black
  regions:
    - page: 0
      x: 0.10
      y: 0.80
      w: 0.40
      h: 0.06
      style:
        mode: text
        text: '[SIGNATURE REMOVED]'

general:
  fields:
    person_name:
      labels: [Full Name, Name]
      max_lines: 1
    postal_address:
      labels: [Postal Address, Address]
      max_lines: 5
    account_number:
      labels: [Account Number]
      max_lines: 1
  stop_labels: [Date, Email, Phone, Total, Notes, Signature]
```

Use the working [masking.yaml](masking.yaml) example:

```sh
uv run python main.py plan input.pdf -o masks.yaml --config examples/masking.yaml
uv run python main.py apply input.pdf --masks masks.yaml -o redacted.pdf
```

`--config` is an alias for `--plugin-config`. Existing configurations without `masking` still produce black masks.

## Modes

| Mode | Result | Options |
| --- | --- | --- |
| `black` | Solid opaque black | No extra options |
| `fill` | Solid opaque color | `background` (default `'#FFFFFF'`) |
| `text` | Opaque background plus a user-supplied label | `text` (default `XXXXX`), `background` (default white), `foreground` (default black), optional `font_path` |

Colors must be quoted `'#RRGGBB'` values. Transparent fills are not supported. For whiteout, use `mode: fill` and `background: '#FFFFFF'`.

Replacement strings are literal, single-line text of up to 200 characters. They do not interpolate or preserve original values. Use a short label: long strings shrink to fit and can become hard to read in a small region. Default fonts support ASCII replacements. For other characters, specify a TrueType/OpenType `font_path` with the required glyphs; the font must be available when exporting. Relative font paths resolve against the configuration directory and are saved as absolute paths in the plan.

The exporter creates a fresh opaque patch, writes the replacement onto it, and pastes it over the source pixels. Text is fitted and clipped to the mask. The exported PDF contains only the resulting page images, with no source text layer or original image hidden underneath.

## Per-field styles

`masking.fields` keys are the semantic finding names returned by detectors: for example `person_name` from the general config, `ship_to` from the default purchase-order detector, or `shipping_name_and_address` from the bundled PO configuration.

Style selection follows this priority:

1. Qualified finding name, such as `general.person_name` or `purchase-order.ship_to`.
2. Unqualified field name, such as `person_name`.
3. `masking.default`.
4. Built-in black default when configuration is absent.

Each override is a complete style with its own mode defaults, not a partial merge with the document default. Field names are matched exactly. A field style does not itself detect anything; the detector must produce that finding. Check the plan's `findings` list to confirm the names.

Text replacements combine contiguous selected words on the same OCR line, producing one label for a name such as `Jane Smith`. Multiline fields get one replacement per selected line. Unselected words and separate columns split replacement spans. Black and fill styles use word-level rectangles, except for joined field tokens where character-backed slices preserve neighboring labels. Mask padding on a slice is bounded by neighboring characters.

## Explicit regions

Regions work without OCR or a plugin. See [regions.yaml](regions.yaml):

```sh
uv run python main.py redact input.pdf -o redacted.pdf --config examples/regions.yaml
```

Each region requires zero-based `page` and normalized `x`, `y`, `w`, `h` coordinates, measured from the top-left of the rendered page. `style` is optional and defaults to `masking.default`. Region coordinates are exact; the word-based `--padding` option does not expand them. Change the example coordinates for your actual PDF.

Regions can also be combined with detector results in one config. Text/fill masks are applied in their saved order, with explicit regions after detected masks. **Black masks always win overlaps**, even when a later region requests text or a fill. This prevents replacement labels from visually undoing black masks. Among text/fill masks, the last overlapping mask wins. Every mask erases original pixels regardless of overlap order.

## Reviewable plans

Plans that contain explicit styles use `version: 2`. Each mask optionally records its resolved `style`; `apply` needs no detector/config to reproduce it. Version-1 black-only plans remain supported.

Example entry you can edit in a reviewed plan:

```yaml
page: 0
x: 0.1
y: 0.2
w: 0.3
h: 0.05
style:
  mode: text
  text: XXXXX
  background: '#FFFFFF'
  foreground: '#000000'
```

A mask without a style is black. Original text is not saved in the plan by this feature; OCR extraction remains a separate `ocr` command. Review the output before sharing, particularly small replacement labels and fields detected from imperfect OCR.
