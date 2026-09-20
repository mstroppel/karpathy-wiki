---
name: wiki-analysis-save
description: Speichert eine fertige Analyse als Wiki-Seite unter analyses/ und erzeugt eine verlinkbare, druckoptimierte HTML-Ansicht, aus der der Browser ein PDF erzeugt. NUR bei ausdrücklicher Aufforderung, eine Analyse im Wiki zu speichern, abzulegen, zu exportieren oder als PDF verfügbar zu machen, verwenden.
---

# Analyse im Wiki speichern

Persistiere eine bereits ausgearbeitete wissenschaftliche Analyse als
Wiki-Seite und erzeuge zusätzlich eine eigenständige Druckansicht. Arbeite und
berichte auf Deutsch. Verändere niemals etwas unter `/knowledge/sources` und
erwähne keine vertraulichen Systempfade in Wiki-Inhalten.

## Ablauf

1. Prüfe `git status` und die jüngste Historie. Verwirf oder überschreibe keine
   fremden Änderungen. Lies `index.md` und `log.md`.
2. Ermittle Titel und einen eindeutigen Slug für die Analyse: kleingeschrieben,
   Kebab-Case, ASCII ohne Umlaute, ohne Leerzeichen. Existiert
   `analyses/<slug>.md` bereits, aktualisiere genau diese Seite und ihre
   Druckansicht, statt eine zweite anzulegen.
3. Schreibe die vollständige Analyse nach `wiki/analyses/<slug>.md`. Beginne mit
   einer `# <Titel>`-Zeile und übernimme den Analysetext inhaltlich unverändert;
   verbessere nur Überschriften, Absätze und Listenstruktur. Setze Struktur mit
   Zwischenüberschriften (Kurzantwort, Befunde, Unsicherheiten, Quellenliste)
   um und behandle Quellenabschnitte wie „Quellen" und „Referenzen" als
   Listen. Verlinke Wiki-Seiten als Wikilinks der Form
   `[[pfad/seite|Bezeichnung]]` und externe Quellen als Markdownlinks. Verlinke
   Wiki-Seiten nicht mit der öffentlichen URL, wenn ein Wikilink ausreicht.
4. Ergänze direkt unter der Titelzeile einen Metadatenabsatz mit
   Erstellungsdatum (`YYYY-MM-DD`) und dem Druckansichtslink als Markdownlink
   der Form `[Druckansicht öffnen](<WIKI_PUBLIC_URL>/.fs/assets/analyses/<slug>.html)`.
   Ersetze `<WIKI_PUBLIC_URL>` durch die öffentliche Wiki-URL aus `AGENTS.md`.
5. Erzeuge `wiki/assets/analyses/<slug>.html` aus demselben Inhalt gemäß dem
   untenstehenden Template. Ersetze ausschließlich `{{titel}}`, `{{datum}}`,
   `{{seiten_url}}`, `{{wiki_url}}`, `{{wiki_name}}` und `{{inhalt}}`. Wandle
   das Markdown in semantisches HTML um: `h2`/`h3` für Zwischenüberschriften,
   Absätze, `ul`/`ol`/`li`, `table` mit `thead`, `blockquote`, `pre`/`code` und
   `a`-Verweise. Wikilinks werden in der HTML-Ansicht zu Markdownlinks mit der
   öffentlichen Wiki-URL (Pfad ohne `.md`). Entferne Frontmatter und
   SilverBullet-spezifische Syntax vollständig.
6. Aktualisiere `index.md`: ergänze unter `## Analysen` den Eintrag
   `- [[analyses/<slug>|<Titel>]]` alphabetisch einsortiert. Ergänze in
   `log.md` einen Eintrag `## <datum> — Analyse gespeichert` mit Titel, Pfad
   und Commit-Hash. Ergänze bei Bedarf in `overview.md` einen Link auf die
   neue Seite.
7. Prüfe den vollständigen Diff. Stelle sicher, dass keine Datei unter
   `/knowledge/sources` verändert wurde und dass Wikilinks auflösen. Erstelle
   genau einen fokussierten Conventional Commit:
   `feat(analyses): <titel> speichern`.
8. Berichte mit Commit-Hash, geänderten Seiten, dem Link zur Wiki-Seite
   `<WIKI_PUBLIC_URL>/analyses/<slug>` und dem Link zur Druckansicht. Weise
   darauf hin, dass die Druckansicht im Browser über „Drucken → Als PDF
   speichern" als PDF-Datei heruntergeladen werden kann.

## Druckansicht Template

Das HTML-Dokument ist eigenständig, ohne externe Abhängigkeiten, und folgt
exakt dieser Struktur. Sprachattribut ist `lang="de"`. Das `{{inhalt}}`-Fragment
beginnt nicht mit einer `h1`; der Titel steht ausschließlich im `header`. Das
Datum ist `YYYY-MM-DD`; `{{wiki_name}}` ist der Name aus `AGENTS.md`;
`{{seiten_url}}` ist die öffentliche URL der Analysenseite; `{{wiki_url}}` ist
die öffentliche Basis-URL aus `AGENTS.md`.

```html
<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{titel}}</title>
<style>
:root {
  --ink: #1a202c;
  --muted: #4a5568;
  --accent: #2b6cb0;
  --border: #cbd5e0;
  --surface: #f7fafc;
}
* { box-sizing: border-box; }
body {
  margin: 0 auto;
  padding: 2.5rem 1.25rem 4rem;
  max-width: 46rem;
  font-family: Georgia, "Times New Roman", serif;
  font-size: 12pt;
  line-height: 1.65;
  color: var(--ink);
  background: #ffffff;
}
.doc-header {
  border-bottom: 2px solid var(--ink);
  margin-bottom: 2rem;
  padding-bottom: 1rem;
}
.doc-header h1 {
  font-family: "Helvetica Neue", Arial, sans-serif;
  font-size: 1.55rem;
  line-height: 1.25;
  margin: 0 0 .4rem;
}
.doc-meta {
  color: var(--muted);
  font-size: .85rem;
  font-family: "Helvetica Neue", Arial, sans-serif;
}
.toolbar {
  position: fixed;
  top: 1rem;
  right: 1rem;
}
.toolbar button {
  font-family: "Helvetica Neue", Arial, sans-serif;
  font-size: .9rem;
  padding: .5rem 1rem;
  border: 1px solid var(--accent);
  border-radius: 6px;
  background: var(--accent);
  color: #ffffff;
  cursor: pointer;
}
.toolbar button:hover { filter: brightness(1.1); }
main h2 {
  font-family: "Helvetica Neue", Arial, sans-serif;
  font-size: 1.15rem;
  margin: 1.8rem 0 .5rem;
  padding-bottom: .2rem;
  border-bottom: 1px solid var(--border);
}
main h3 {
  font-family: "Helvetica Neue", Arial, sans-serif;
  font-size: 1rem;
  margin: 1.4rem 0 .4rem;
}
main p { margin: .6rem 0; }
main ul, main ol { margin: .6rem 0; padding-left: 1.5rem; }
main li { margin: .25rem 0; }
main a { color: var(--accent); text-decoration: none; overflow-wrap: anywhere; }
main a:hover { text-decoration: underline; }
main blockquote {
  margin: 1rem 0;
  padding: .25rem 1rem;
  border-left: 3px solid var(--border);
  color: var(--muted);
}
main code {
  font-family: ui-monospace, "Cascadia Code", "Source Code Pro", Menlo, monospace;
  font-size: .88em;
  background: var(--surface);
  padding: .08em .3em;
  border-radius: 3px;
}
main pre {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: .75rem 1rem;
  overflow-x: auto;
  font-size: .88em;
}
main pre code { background: none; padding: 0; }
main table {
  border-collapse: collapse;
  width: 100%;
  margin: 1rem 0;
  font-size: .92em;
}
main th, main td {
  border: 1px solid var(--border);
  padding: .35rem .6rem;
  text-align: left;
  vertical-align: top;
}
main th { background: #edf2f7; font-family: "Helvetica Neue", Arial, sans-serif; }
main hr { border: none; border-top: 1px solid var(--border); margin: 1.5rem 0; }
.doc-footer {
  margin-top: 2.5rem;
  padding-top: .75rem;
  border-top: 1px solid var(--border);
  color: var(--muted);
  font-size: .8rem;
  font-family: "Helvetica Neue", Arial, sans-serif;
}
@media print {
  @page { size: A4; margin: 20mm 18mm; }
  body { max-width: none; padding: 0; font-size: 10.5pt; }
  .toolbar { display: none; }
  main h2, main h3 { break-after: avoid-page; page-break-after: avoid; }
  main p, main li, main tr, main blockquote { break-inside: avoid-page; page-break-inside: avoid; }
  main a[href^="http"]::after {
    content: " (" attr(href) ")";
    font-size: .78em;
    color: var(--muted);
    overflow-wrap: anywhere;
  }
}
</style>
</head>
<body>
<div class="toolbar">
  <button type="button" onclick="window.print()">Als PDF speichern</button>
</div>
<header class="doc-header">
  <h1>{{titel}}</h1>
  <div class="doc-meta">Analyse vom {{datum}} · <a href="{{seiten_url}}">{{seiten_url}}</a></div>
</header>
<main>
{{inhalt}}
</main>
<footer class="doc-footer">
  Aus {{wiki_name}} erzeugte Analyse · Quelle: <a href="{{seiten_url}}">{{seiten_url}}</a>
</footer>
</body>
</html>
```

Wende das Template unverändert an. Passe nur `{{...}}-Platzhalter an; ändere
keine CSS-Regeln und erfinde keine weiteren Formatierungen, damit alle
Druckansichten im Wiki einheitlich aussehen.
