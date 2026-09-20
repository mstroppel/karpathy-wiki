# Analysis Export

Analyses produced with OpenCode can be persisted in the wiki, shared by link,
and downloaded as a PDF. No additional service is required: the saved analysis
is a regular wiki page served by SilverBullet, and the printable view is a
plain HTML file inside the wiki space.

## Save an Analysis

Run a scientific analysis first, for example with the `/analyse` command, or
bring your own analysis text into the conversation. Then either ask OpenCode to
save the analysis (for example "Speichere diese Analyse im Wiki") or invoke the
`/analyse-save` command with the analysis text as its argument.

The `wiki-analysis-save` skill then performs one focused commit that:

- writes the analysis to `wiki/analyses/<slug>.md`,
- generates a self-contained, print-optimized HTML document at
  `wiki/assets/analyses/<slug>.html`,
- updates `index.md`, `log.md`, and optionally `overview.md`.

Saving an existing slug updates the same page and its print view instead of
creating a duplicate.

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
`wiki/AGENTS.md` manually, as described in [migration](migration.md).
