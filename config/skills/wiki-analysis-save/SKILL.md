---
name: wiki-analysis-save
description: Speichert eine fertige Analyse als Wiki-Seite und druckoptimierte HTML-Ansicht auf ausdrücklichen Auftrag.
---

# Analyse speichern

Arbeite und berichte auf Deutsch. Speichere nur eine bereits fertige Analyse;
verändere keine Quellen und veröffentliche keine internen Systempfade.
Ist im Auftrag kein vollständiger Analysetext enthalten, speichere nichts und
melde den fehlenden Text. Erfinde keine fehlenden Aussagen oder Quellen.
Enthält der übergebene Text einen Kürzungsmarker aus einer Tool-Ausgabe
(`[showing ...; full output saved to ...]`) oder erkennbare Auslassungen,
speichere nichts und melde, dass der vollständige Text erneut übergeben werden
muss.

Prüfe zuerst mit `test -w /knowledge/wiki`, ob Schreibzugriff besteht. Ohne
Schreibzugriff (Read-only-Laufzeit) führe ausschließlich den Abschnitt
„Vorbereiten im vertrauenswürdigen Kanal“ aus; die Schritte 1 bis 4 sind dann
nicht ausführbar.

1. Lies `AGENTS.md`, `index.md`, `log.md`, Git-Status und Historie. Erhalte
   fremde Änderungen. Wähle einen eindeutigen ASCII-Kebab-Case-Slug; wenn
   `analyses/<slug>.md` existiert, aktualisiere diese Seite und Druckansicht.
2. Schreibe die vollständige Analyse nach `analyses/<slug>.md` mit `# <Titel>`
   in Zeile 1 und `Erstellt: YYYY-MM-DD` in Zeile 2 sowie Befunden,
   Unsicherheiten und Quellen.
   Erhalte ihre Aussagen, strukturiere nur die Darstellung. Verwende interne
   `[[pfad/seite|Bezeichnung]]`-Wikilinks und externe Markdownlinks. Ergänze
   `[Druckansicht öffnen](<WIKI_PUBLIC_URL>/.fs/assets/analyses/<slug>.html)`.
3. Führe nach jedem Schreiben oder Aktualisieren der Markdown-Seite im
   Wiki-Verzeichnis `render-analysis analyses/<slug>.md --wiki-url
   '<WIKI_PUBLIC_URL>'` aus. Verwende die öffentliche Wiki-URL aus `AGENTS.md`,
   keine beispielhafte URL. Das Skript erzeugt
   `assets/analyses/<slug>.html` aus der Markdown-Seite und der Druckvorlage.
   Prüfe den Exit-Status und den erzeugten Inhalt; bei einem Fehler nicht
   committen. Bearbeite das HTML nicht manuell.
4. Trage die Seite alphabetisch unter `## Analysen` in `index.md` ein; ergänze
   einen Logeintrag mit Datum, Titel und Pfad. Verlinke sie bei Bedarf in
   `overview.md`. Prüfe Diff und Links und erstelle genau einen fokussierten
   Commit `feat(analyses): <titel> speichern`.

Melde Commit-Hash, geänderte Seiten, öffentliche Wiki- und Druckansichts-URL.
Die Druckansicht kann im Browser als PDF gespeichert werden. Bei einem Fehler
melde die unerledigten Schritte statt Erfolg.

## Vorbereiten im vertrauenswürdigen Kanal

Ohne Schreibzugriff bereitest du die Übernahme ausschließlich vor; der
vertrauenswürdige Kanal schreibt Seite, Druckansicht, `index.md` und `log.md`
und erstellt den Commit.

1. Führe die Schritte 1 und 2 lesend aus: vollständigen Analysetext bestimmen,
   eindeutigen ASCII-Kebab-Case-Slug wählen, Seite mit `# <Titel>` in Zeile 1
   und `Erstellt: YYYY-MM-DD` in Zeile 2 strukturieren. Schreibe nichts,
   rendere nichts, ändere weder Index noch Log.
2. Zeige die vollständige Seite als Vorschau und weise auf die Bestätigung
   durch `/analysis-save` hin.
3. Gib als Abschluss deiner Antwort genau einen Block aus drei Zeilen aus:
   eine Fence-Zeile mit der Kennung `karpathy-authoring`, eine Zeile mit dem
   einzeiligen JSON-Objekt `{"kind":"analysis","target":"<slug>","content":"<vollständige Seite>"}`,
   eine Fence-Zeile. `content` ist die Seite als JSON-String;
   Kürzungsmarker, `[` `]` oder `|` im Titel sowie Titel über 200 Zeichen
   sind unzulässig.
4. Der Nutzer sendet `/analysis-save`. Der Kanal übernimmt genau diesen Block;
   du speicherst danach nichts nach. Bei Blockierung melde den gemeldeten
   Blocker und die offenen Schritte; führe keine Reparatur aus.
