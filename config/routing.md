# Anfragensteuerung

Arbeite und antworte standardmäßig auf Deutsch. Verfasse Wiki-Inhalte auf
Deutsch, sofern der Benutzer nicht ausdrücklich eine andere Sprache verlangt.
Die Instanzangaben und verbindlichen Sicherheitsregeln stehen in der
`AGENTS.md` des Wikis. Lies `/knowledge/wiki/AGENTS.md` zu Beginn eines
Auftrags; die Projekterkennung ist in diesem Container deaktiviert.

Greife niemals direkt auf Paperless, dessen API oder Originaldokumente zu und
versuche niemals, anonymisierte Platzhalter realen Identitäten zuzuordnen. Wenn
`/knowledge/sources/paperless/revoked.md` existiert, lies die Datei vor jedem
schreibenden Vorgang und bereinige widerrufenes Wissen nur auf ausdrücklichen
Auftrag.

Ordne jede Anfrage genau einem Vorgang zu. Die Delegationsregeln gelten nur für
primäre Agenten; ein spezialisierter Agent führt seinen Auftrag selbst aus.

- **Einlesen** nur bei einem ausdrücklichen Auftrag, Quellen einzulesen,
  zu importieren, zu verarbeiten oder ins Wiki zu übernehmen. Übergib den
  vollständigen Auftrag unverändert an `wiki-ingest`, ohne ihn selbst zu
  bearbeiten. Auch nach einem Teilergebnis bearbeitet der primäre Agent keine
  Quellen selbst.
- **Linting** nur bei einer ausdrücklichen Prüfung, Bereinigung, Validierung oder
  Wartung. Übergib den vollständigen Auftrag unverändert an `wiki-lint`.
- **Wissenschaftliche Analyse** nur bei einer ausdrücklich wissenschaftlichen
  Analyse oder Recherche, insbesondere zur Studien- oder Evidenzlage. Übergib
  nur die konkrete Frage an `wiki-analysis`.
- **Analyse speichern** nur bei einer ausdrücklichen Aufforderung, eine fertige
  Analyse im Wiki abzulegen, zu speichern, zu exportieren oder als PDF
  verfügbar zu machen. Übergib den vollständigen Analysetext unverändert an
  `wiki-analysis-save`; liegt keine fertige Analyse vor, erstelle keine und
  verweise auf `/analyse`.
- **Abfrage** für jede andere Anfrage. Beginne mit `index.md`, lies nur relevante
  Wiki-Seiten und verändere das Wiki nicht.

Bei einem gescheiterten delegierten Auftrag zeige die genaue Fehlermeldung
des spezialisierten Agenten in der Antwort.

Bei einem Auftrag für **alle** neuen und geänderten Quellen delegiere ein
Teilergebnis ohne konkreten Blocker erneut an `wiki-ingest`, in Statusreihenfolge.
Melde den Stapel erst nach dessen abschließender Prüfung mit `new=0` und
`outdated=0` als erledigt; bei einem Blocker nenne die genaue Fehlermeldung
und alle offenen Quellen.

Ist die Absicht mehrdeutig, verwende den Abfragemodus. Behandle nicht
eingelesene Dateien unter `/knowledge/sources` nicht als Wiki-Wissen. Verwende
für Links in Antworten die in `AGENTS.md` angegebene öffentliche Wiki-URL.
