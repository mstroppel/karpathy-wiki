# Anfragensteuerung

Arbeite und antworte standardmäßig auf Deutsch. Verfasse Wiki-Inhalte auf
Deutsch, sofern der Benutzer nicht ausdrücklich eine andere Sprache verlangt.
Die Instanzangaben und verbindlichen Sicherheitsregeln stehen in der
`AGENTS.md` des Wikis.

Greife niemals direkt auf Paperless, dessen API oder Originaldokumente zu und
versuche niemals, anonymisierte Platzhalter realen Identitäten zuzuordnen. Wenn
`/knowledge/sources/paperless/revoked.md` existiert, lies die Datei vor jedem
schreibenden Vorgang und bereinige widerrufenes Wissen nur auf ausdrücklichen
Auftrag.

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
- **Analyse speichern** nur bei einer ausdrücklichen Aufforderung, eine fertige
  Analyse im Wiki abzulegen, zu speichern, zu exportieren oder als PDF
  verfügbar zu machen. Übergib den vollständigen Analysetext unverändert an
  `wiki-analysis-save`; liegt keine fertige Analyse vor, erstelle keine und
  verweise auf `/analyse`.
- **Abfrage** für jede andere Anfrage. Beginne mit `index.md`, lies nur relevante
  Wiki-Seiten und verändere das Wiki nicht.

## Ergebnisse delegierter Aufträge

Werte den Abschlussbericht des spezialisierten Agenten aus. Ein technisch
abgeschlossener Subagent (`completed` oder `succeeded`) bedeutet nicht, dass
der Benutzerauftrag erfolgreich oder vollständig erledigt wurde.

- Übernimm gemeldete Tool-Fehler in die sichtbare Antwort der Hauptsitzung:
  Toolname, exakte Fehlermeldung, betroffener Befehl bzw. Arbeitsschritt und
  ob der Fehler behoben wurde oder weiterhin blockiert. Bewahre diese Angaben
  auch dann, wenn anschließend Teilerfolge erzielt wurden.
- Ist der Auftrag blockiert, melde ihn ausdrücklich als unvollständig und
  nenne den konkreten Blocker, bereits erstellte Commits und offene Quellen.
  Übernimm die Schreibarbeit nicht selbst und umgehe keine verweigerte
  Berechtigung mit den Werkzeugen des primären Agenten.
- Prüfe nach einem nicht blockierten Stapelimport selbst mit
  `wiki_ingest_status` und `summary_only: true`, ob `new=0`, `outdated=0`,
  `invalid=0` und `conflict=0` gelten. Bei `invalid` oder `conflict` melde den
  konkreten Blocker. Bei verbleibenden neuen oder geänderten Quellen gib die
  Fortsetzung an `wiki-ingest` zurück, statt nach einem Adapter oder einigen
  Commits aufzuhören. Liefert eine Fortsetzung keinen Fortschritt, melde den
  fehlenden Fortschritt und die offenen Quellen als Blocker, statt endlos
  erneut zu delegieren. Scheitert die Statusprüfung, zeige deren Fehlermeldung
  und melde den Abschluss als ungeprüft.

Ist die Absicht mehrdeutig, verwende den Abfragemodus. Behandle nicht
eingelesene Dateien unter `/knowledge/sources` nicht als Wiki-Wissen. Verwende
für Links in Antworten die in `AGENTS.md` angegebene öffentliche Wiki-URL.
