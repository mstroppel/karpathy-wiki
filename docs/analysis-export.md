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
