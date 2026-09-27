---
name: wiki-lint
description: Prüft und wartet das Wiki auf ausdrücklichen Auftrag.
---

# Wiki prüfen

Arbeite und berichte auf Deutsch. Lies `AGENTS.md`, `index.md`, `overview.md`,
`log.md` und alle Wiki-Markdown-Seiten; prüfe vor Änderungen Git-Status und
Historie. Prüfe Wikilinks, Index und verwaiste Seiten, Duplikate, Dateinamen,
Herkunftsnachweise, Widersprüche und veraltete Synthesen. Paperless-Seiten mit
`paperless_id` brauchen ein HTTPS-`paperless_url`-Feld und einen sichtbaren Link.

Rufe `wiki_ingest_status` für alle Adapter auf. Melde neue, veraltete,
ungültige, widersprüchliche, verwaiste und widerrufene Revisionen; bereinige
Widerrufe nur auf ausdrücklichen Auftrag. Belege Befunde anhand der betroffenen
Seiten; erfinde keine fehlenden Fakten oder Identitäten.

Wende nur sichere, belegtreue Korrekturen an. Aktualisiere bei Änderungen
`index.md` und `overview.md` nach Bedarf und ergänze einen Prüfungseintrag in
`log.md`. Prüfe den vollständigen Diff, Links und Herkunftsnachweise; verändere
nichts unter `/knowledge/sources`. Erstelle bei Änderungen genau einen Commit
`fix(wiki): wissensbestand prüfen`, nie einen leeren Commit.

Nenne geänderte Seiten, Commit-Hash und verbleibende Widersprüche und Lücken.
Falls ein erforderlicher Schritt scheitert, melde Befehl, Fehler und offene
Prüfschritte als unvollständig statt die Prüfung als abgeschlossen auszugeben.
