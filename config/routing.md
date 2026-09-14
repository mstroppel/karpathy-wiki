# Anfragensteuerung

Arbeite und antworte standardmäßig auf Deutsch. Verfasse Wiki-Inhalte auf
Deutsch, sofern der Benutzer nicht ausdrücklich eine andere Sprache verlangt.
Die Instanzangaben und verbindlichen Sicherheitsregeln stehen in der
`AGENTS.md` des Wikis.

Ordne jede Anfrage genau einem Vorgang zu. Die Delegationsregeln gelten nur für
primäre Agenten; ein spezialisierter Agent führt seinen Auftrag selbst aus.

- **Einlesen** nur bei einem ausdrücklichen Auftrag, Quellen einzulesen,
  zu importieren, zu verarbeiten oder ins Wiki zu übernehmen. Übergib den
  vollständigen Auftrag unverändert an `wiki-ingest`, ohne ihn vorher selbst zu
  bearbeiten.
- **Linting** nur bei einer ausdrücklichen Prüfung, Bereinigung, Validierung oder
  Wartung. Übergib den vollständigen Auftrag unverändert an `wiki-lint`.
- **Wissenschaftliche Analyse** nur bei einer ausdrücklich wissenschaftlichen
  Analyse oder Recherche, insbesondere zur Studien- oder Evidenzlage. Übergib
  nur die konkrete Frage an `wiki-analysis`.
- **Abfrage** für jede andere Anfrage. Beginne mit `index.md`, lies nur relevante
  Wiki-Seiten und verändere das Wiki nicht.

Ist die Absicht mehrdeutig, verwende den Abfragemodus. Behandle nicht
eingelesene Dateien unter `/knowledge/sources` nicht als Wiki-Wissen. Verwende
für Links in Antworten die in `AGENTS.md` angegebene öffentliche Wiki-URL.
