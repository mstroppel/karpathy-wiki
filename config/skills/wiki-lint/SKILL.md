---
name: wiki-lint
description: Prüft, auditiert, bereinigt und validiert das vollständige Wiki. NUR bei ausdrücklichem Wiki-Linting, Audit, Prüfung, Bereinigung, Validierung, Wartung oder Gesundheitscheck verwenden.
---

# Wiki Prüfen

Prüfe `/knowledge/wiki` als zusammenhängenden Wissensbestand und wende nur
sichere, belegtreue Korrekturen an. Arbeite und berichte auf Deutsch.

Prüfe defekte Wikilinks, Indexabweichungen, verwaiste Seiten, Duplikate,
ungültige Dateinamen, fehlende Herkunftsnachweise, unaufgelöste Widersprüche,
veraltete Synthesen und Abweichungen zwischen Überblick, Themen- und
Quellenseiten. Rufe `wiki_ingest_status` auf und prüfe WebDAV sowie alle
aktivierten Adapter auf ungültige, widersprüchliche, veraltete, verwaiste oder
widerrufene Revisionen; bereinige Widerrufe nie ohne ausdrücklichen Auftrag.

1. Lies `AGENTS.md`, `index.md`, `overview.md`, `log.md` und danach alle
   Markdown-Seiten im Wiki.
2. Prüfe Git-Status und Historie und untersuche genügend verknüpfte Seiten, um
   jeden Befund zu belegen. Rate keine fehlenden Fakten oder Identitäten.
3. Wende sichere Struktur- und Konsistenzkorrekturen an. Aktualisiere bei Bedarf
   Index und Überblick und ergänze genau einen Prüfungseintrag im Protokoll.
4. Prüfe den vollständigen Diff, Links und Herkunftsnachweise. Stelle sicher,
   dass keine Datei unter `/knowledge/sources` verändert wurde.
5. Erstelle bei Änderungen genau einen Commit mit
   `fix(wiki): wissensbestand prüfen`. Erzeuge keinen leeren Commit.

Nenne geänderte Seiten und Commit-Hash sowie alle verbleibenden Widersprüche,
fehlenden Quellenangaben und Wissenslücken.
