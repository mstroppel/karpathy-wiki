---
name: wiki-lint
description: Prüft und wartet das Wiki auf ausdrücklichen Auftrag.
---

# Wiki prüfen

Arbeite und berichte auf Deutsch. Lies `AGENTS.md`, `index.md`, `overview.md`,
`log.md` und alle Wiki-Markdown-Seiten; bei gezielter Git-Prüfung oder Reparatur
lies nur die betroffenen Seiten. Prüfe vor Änderungen Git-Status und
Historie. Prüfe Wikilinks, Index und verwaiste Seiten, Duplikate, Dateinamen,
Herkunftsnachweise, Widersprüche und veraltete Synthesen. Paperless-Seiten mit
`paperless_id` brauchen ein HTTPS-`paperless_url`-Feld und einen sichtbaren Link.

Rufe `wiki_ingest_status` für alle Adapter auf. Melde neue, veraltete,
ungültige, widersprüchliche, verwaiste und widerrufene Revisionen; bereinige
Widerrufe nur auf ausdrücklichen Auftrag. Belege Befunde anhand der betroffenen
Seiten; erfinde keine fehlenden Fakten oder Identitäten.

## Bereinigung

- Prüfe Git-Status und staged Diff. Benenne fremde Änderungen und frage nach
  separatem Commit, Prüfung oder Abbruch; übernimm oder verwerfe sie nur nach
  ausdrücklicher Zustimmung. Bei Konflikten erst den konkreten Plan bestätigen.
- Repariere Metadaten nur mit eindeutig belegter Quellenidentität und historisch
  beabsichtigter Revision (Auftrag, Quellen-Commit und Quellhash). Eine aktuelle
  Revision allein belegt keine erneute Auswertung alter Inhalte. Bei fehlender
  Evidenz frage nach; markiere nichts durch bloßes Umsetzen des Hashes als aktuell.
- Bei `revoked` prüfe auch abgeleitete Aussagen und Links; bestätige den konkreten
  Entfernungsvorschlag. Bei `orphaned` kläre die Ursache und frage nach Behalten,
  Kennzeichnen oder belegter Entfernung. Fehlende Quellen sind kein Löschauftrag.
- Bereits committed Fehler erhalten einen separaten Korrekturcommit; erhalte
  Historie und Quellen. Prüfe danach Herkunft und Status erneut und nenne
  verbleibende Blocker sowie den Git-Status für die Ingest-Fortsetzung.

Wende nur sichere, belegtreue Korrekturen an. Aktualisiere bei Änderungen
`index.md` und `overview.md` nach Bedarf und ergänze einen Prüfungseintrag in
`log.md`. Prüfe den vollständigen Diff, Links und Herkunftsnachweise; verändere
nichts unter `/knowledge/sources`. Erstelle bei Änderungen genau einen Commit
`fix(wiki): wissensbestand prüfen`, nie einen leeren Commit.

Nenne geänderte Seiten, Commit-Hash und verbleibende Widersprüche und Lücken.
Falls ein erforderlicher Schritt scheitert, melde Befehl, Fehler und offene
Prüfschritte als unvollständig statt die Prüfung als abgeschlossen auszugeben.
