# Excel tables through WebDAV

Enable and configure the existing `webdav` profile and `REDACTIONS_FILE` as
described in [configuration](configuration.md#secrets). Upload an unencrypted
`.xlsx` to `WEBDAV_PATH`, alongside ordinary Markdown/HTML sources. **Never put
files directly in `sources/webdav`.** Originals remain in private
`incoming/webdav`; only the locally extracted, redacted Markdown is published.
Then use normal manual wiki ingest (`/ingest-new` or an explicitly selected
source). This does not schedule wiki ingest or introduce another service.

## Extracted content

- Workbook identity (relative upload path), worksheet names, headers and cell
  values are retained. Tables use Excel column letters and row numbers; each
  sheet records its bounding range. Interior blank cells/rows stay in position;
  empty outer ranges and formatting-only cells are omitted.
- Only visible worksheets, rows and columns are included. Hidden/very-hidden
  sheets, hidden rows/columns (including grouped columns), comments, document
  properties, charts, images and hyperlink targets are omitted. Merged ranges
  are listed explicitly: only the anchor carries a value; other merged cells
  are marked, not populated with duplicated values. A hidden anchor is not
  copied to visible cells. Every extract states these omissions.
- Numbers, booleans, Excel errors, ISO dates/times and durations are labelled.
  Numeric formats `General`, `0`, `#,##0`, up to six decimal zeroes, a trailing
  `%`, or a trailing quoted unit (e.g. `0.00 "kg"`) are supported. Group separators
  are normalized away. Other numeric formats keep the underlying number with an
  explicit **unsupported number format** annotation. Currency/accounting,
  conditional/locale-specific formats and Excel's visual layout are not rendered.
  Dates/times are semantic ISO values, not a reproduction of their Excel display.
- Default-hidden sheet row/column layouts are rejected rather than guessing
  visibility. Malformed or overlapping column definitions are also rejected:
  copy the visible data to a fresh worksheet before uploading.
- Formula cells include their expressions as calculation context and cached
  results with the same type labels. **No formulas are calculated or verified.**
  A missing cache is marked `MISSING cached result`, never replaced by zero;
  Excel errors such as `#DIV/0!` are explicit. Simple formula strings are supported;
  array/data-table formula objects are rejected rather than silently omitted.
  Visible expressions may still name hidden sheets/ranges, and cached results
  may derive from hidden cells: hidden content is not extracted directly.

**Before uploading: open the workbook in Excel, recalculate, then save it.**
Other writers (including openpyxl) may omit formula caches. A cache can still be
stale; the provider cannot establish freshness or correctness.

## Privacy and safety

All included workbook-derived text, including upload identity, sheet names,
number-format annotations, formulas and cached strings, crosses the shared local
deny-list redactor **before** Markdown/HTML escaping. Unknown sensitive values may
remain: inspect the sanitized extract before sending it to a model.

No macros execute and no external links, entities or data connections are fetched.
Legacy `.xls`, `.xlsb` and macro-enabled `.xlsm` are excluded from synchronization;
use an unencrypted `.xlsx` instead. If already present locally, these unsupported
formats are still rejected with export instructions. Encrypted, corrupt or
unsupported packages produce fixed actionable diagnostics, without workbook text
or raw parser errors.
Other non-source formats remain ignored. rclone output is suppressed because its
diagnostics can contain private filenames; sync failures remain visible in health.

Each workbook runs in its own parser subprocess with these fixed limits:

| Resource | Limit |
| --- | --- |
| Compressed input | 20 MiB |
| Total uncompressed ZIP members | 100 MiB |
| ZIP members / XML elements | 2,048 / 1,000,000 |
| Compression ratio for members over 1 MiB | 200:1 |
| Worksheets (including hidden) | 32 |
| Row / column coordinates | 10,000 / 256 |
| Examined row records across workbook | 50,000 |
| Examined cell records / total output bounding rectangles | 200,000 each |
| Merged ranges across workbook | 2,000; each at most 200,000 cells |
| Parser memory / CPU / wall time | 768 MiB / 30 seconds / 45 seconds |
| Private parser result / final UTF-8 Markdown | 4 MiB each |

Limits include hidden content when examining input. Oversized/suspicious workbooks
fail explicitly; there is no successful truncated extract. Split or simplify the
workbook and re-upload. Private quarantine reports use a path hash and a fixed
diagnostic. Any extraction/redaction failure retains the entire last successful
source generation and its manifest, including the previously published workbook;
it does not publish the other files from the failed cycle either.

## Identity, updates and provenance

An XLSX uploaded at relative path `P` publishes to
`generations/<id>/xlsx/<sha256(P UTF-8)>/workbook.md`. Its source key is
`xlsx/<hash>/workbook.md`, with wiki target `webdav/<source-key>/index.md` under
the normal `sources/` wiki subtree. Manifest claims and frontmatter contain the
opaque stable key, never the raw filename. Generation inventory uses the same
path hash; the sanitized heading carries the redacted human-readable identity.

Ordinary Markdown/HTML retain their paths; `table.xlsx` and `table.xlsx.md`
coexist. If an ordinary file deliberately occupies a hashed workbook output
path (or an ancestor/descendant), the cycle fails closed; rename that conflicting
upload. No source can overwrite another output.

Edits and deletions use the existing generation/manifest contract. Revisions are
SHA-256 hashes of sanitized Markdown, so unchanged output keeps the same revision
and claim. Redaction changes regenerate extracts. The code-maintained
`EXTRACTION_VERSION` in `ingest/webdav/src/karpathy_wiki_ingest_webdav/xlsx.py`
also participates in cycle identity/metadata; bump it for extraction, rendering,
dependency or limit changes. There is no change to the manual wiki provenance
contract.
