# Analysis Export

Analyses produced with OpenCode can be persisted in the wiki, shared by link,
and downloaded as a PDF. No additional service is required: the saved analysis
is a regular wiki page served by SilverBullet, and the printable view is a
plain HTML file inside the wiki space.

## Save an Analysis

Run a scientific analysis first, for example with the `/analysis` command, or
bring your own analysis text into the conversation. Then run `/analysis-save`
without arguments to save the latest finished analysis in the same conversation,
including subsequent corrections. You can also pass the complete analysis text
as the command argument to save that text instead, or ask OpenCode to save the
analysis (for example "Speichere diese Analyse im Wiki"). If there is no
finished analysis in the conversation or the command argument, nothing is saved.
The command asks the main agent to pass the complete text to the save subagent,
just like a natural-language save request.
This also adds the analysis to the wiki index and log, making it available as
wiki knowledge. It does not need a separate source-ingest pass: `wiki-ingest`
handles external files under `/knowledge/sources`, not saved analyses.
If a previous analysis came from a subagent and its tool result was shortened,
the main agent must recover the complete saved tool output before delegating
the save. If the full text cannot be recovered, saving stops instead of
publishing an incomplete analysis. `/analysis` runs in the current conversation
and avoids this subagent-result limit.

Saving requires writable runtime mode; reader mode cannot save analyses.

The `wiki-analysis-save` skill then performs one focused commit that:

- writes the analysis to `wiki/analyses/<slug>.md`,
- generates a self-contained, print-optimized HTML document at
  `wiki/assets/analyses/<slug>.html`,
- updates `index.md`, `log.md`, and optionally `overview.md`.

Saving an existing slug updates the same page and its print view instead of
creating a duplicate. The agent writes Markdown with a `# Title` first line and
`Erstellt: YYYY-MM-DD` second line, then runs `render-analysis` on that file
from the wiki checkout. The installed renderer uses the existing print template,
converts headings, links, citations, tables and wiki links, and sanitizes HTML.
Run the renderer again after updating a saved analysis; commit the Markdown and
HTML together. For example:

```sh
render-analysis analyses/example.md --wiki-url 'https://wiki.example.com'
```

## Diagram images

Use ordinary Markdown images with nonempty alternative text. Store each image
inside the asset directory belonging to the analysis:

```text
analyses/example.md
assets/analyses/example/daily-volume.svg
assets/analyses/example/scatter.png
assets/analyses/example.html
```

```markdown
![Daily documented volume, with missing days marked](/.fs/assets/analyses/example/daily-volume.svg)
![Individual measurements](/.fs/assets/analyses/example/scatter.png "Measurements")
```

This root-relative `/.fs/` path addresses a local file in SilverBullet. During
export the renderer reads the corresponding file directly; it makes no HTTP
requests. Only lowercase ASCII kebab-case filenames ending in `.png` or `.svg`
in that analysis's directory are accepted. No subdirectories, symlinks, parent
traversal, query strings, fragments, external URLs, author-provided data URLs,
raw HTML images or inline SVG are supported. Code examples remain ordinary text.

The renderer validates every image, then embeds it as a generated base64 data
URL in an `<img>`. The resulting HTML remains self-contained when downloaded:
diagram display needs neither a network connection nor a wiki login. The wiki
page still uses the original asset files. Publish those assets together with
the Markdown and generated HTML. Image content is published data; PNG metadata
removal is not anonymization of labels or plotted values.

### Formats and limits

- **PNG:** a decoded static PNG, at most 10 MiB and 40 megapixels. It is re-encoded
  without source metadata, preserving transparency. APNG and mismatched/corrupt
  files are rejected. Re-encoded PNGs must also fit the 10 MiB limit.
- **SVG:** at most 2 MiB, 10,000 elements, XML/reference depth 64 and 100,000
  expanded elements. The root and elements must use the SVG namespace. Shapes,
  paths, groups, text, local clipping, patterns, gradients, markers and `use`
  references are supported. Supported elements are `svg`, `g`, `defs`, `title`,
  `desc`, `path`, `rect`, `circle`, `ellipse`, `line`, `polyline`, `polygon`,
  `text`, `tspan`, `clipPath`, `pattern`, `linearGradient`, `radialGradient`,
  `stop`, `marker` and `use`. References must be unambiguous local fragments;
  missing targets, cycles and excessive expansion are rejected. Dimensions must
  be positive and bounded to 20,000 nominal CSS pixels per axis and 40 megapixels
  per viewport; percentages cannot exceed 100%. Relative font units use 16 pixels
  for this conservative check. Numeric magnitudes are bounded to 10 million and
  individual attributes to 100,000 characters.
- Simple SVG presentation attributes and inline `style` declarations are
  supported. A CSS parser validates declarations and converts allowed properties
  to presentation attributes. Stylesheets, CSS variables, imports, `!important`,
  scripts, event handlers, animation, `foreignObject`, embedded images, external
  fonts/resources, DTDs/entities and processing instructions are rejected.
  Unknown elements, attributes and values fail rather than silently dropping
  diagram content. Path commands must have complete argument groups and valid
  arc flags/radii; transforms have checked argument counts and point lists need
  coordinate pairs. The numeric-list subset uses whitespace or single commas;
  compact arc flags must be separated. General-purpose SVG exports may need an
  explicitly supported export preset or a supplied PNG alternative; this is not
  full SVG support.
- **Document:** at most 50 image occurrences and 50 MiB final UTF-8 HTML, including
  base64 expansion. Repeated assets are validated once but count on each use.

Embedded SVG is an isolated static image, not inline document markup. There are
no per-point interactive controls or guaranteed embedded-font fidelity. Keep
full values, uncertainty explanations and source links in a readable HTML table
when the analysis includes them; alternative text is not a replacement.

The existing HTML sanitizer remains in place. Its protocol allowlist does not
accept arbitrary `data:` URLs; only renderer-created, validated image nodes are
inserted after sanitization. Local reads are directory-relative and do not follow
symlinks. The renderer reports the number of embedded PNG and SVG occurrences.
Any unsupported image fails the export with a nonzero exit status. A previously
generated HTML view is replaced atomically only after the whole render succeeds;
on failure it remains the old view, not evidence of a successful current export.

The print template scales images to available width, preserves aspect ratios
and limits tall diagrams for A4 printing. Check the actual browser/PDF output;
very dense diagrams still require appropriate source dimensions and labels.

## Links

Both links are derived from `WIKI_PUBLIC_URL`:

| Content | URL |
| --- | --- |
| Analysis page (view and share) | `WIKI_PUBLIC_URL/analyses/<slug>` |
| Printable view | `WIKI_PUBLIC_URL/.fs/assets/analyses/<slug>.html` |

The printable view is a styled, standalone HTML page with an embedded
"Als PDF speichern" button. Opening it and using the browser's print dialog
("Drucken → Als PDF speichern") produces the PDF; A4 page margins, page breaks,
and cited URLs are styled for print output. The URL follows the same
authentication rules as the rest of `WIKI_PUBLIC_URL`.

## Existing Installations

The `AGENTS.md` generated during first initialization lists the delegation
targets. Add the `wiki-analysis-save` delegation sentence to an existing
`wiki/AGENTS.md` manually.
