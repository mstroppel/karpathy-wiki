# Implementierungsplan: #157 – automatisches `/ingest-new`

Issue: <https://github.com/mstroppel/karpathy-wiki/issues/157>

Branch: `feat/157-auto-ingest-new`

Planungsstand: 2026-10-09, ausgehend von `247c378` auf `main`.

## Ziel und Grenzen

Neue und veraltete, bereits bereinigte Quellen werden nach ausdrücklichem
Opt-in automatisch mit dem bestehenden `/ingest-new` verarbeitet. Ohne Opt-in
bleibt das heutige Verhalten bestehen. Dieser Branch enthält zunächst nur die
Planung; eine Implementierung ist noch nicht enthalten.

- #152 ist abgeschlossen. Aktuell gilt: genau eine Quelle pro frischem Worker,
  sequenzielle Verarbeitung, privates Journal und dateibasierter Bericht.
  Kontextschätzungen sind seit #181 weiche Warnziele, keine harten Abbruchgrenzen.
- Keine zweite Ingest-Implementierung, keine Parallelisierung von Wiki-Writern
  und keine Abhängigkeit vom zurückgestellten transaktionalen Publisher #113.
- Quellen bleiben unveränderliche, nicht vertrauenswürdige Daten. Keine
  zusätzlichen Modellrechte, Provider-Zugriffe oder Offenlegung von Zugangsdaten.
- Keine Migrationen, Kompatibilitätsaliase oder automatischen Datenverschiebungen.
- `revoked` und `orphaned` werden weiterhin berichtet, nicht automatisch bereinigt.
- Zusätzlich vereinbart: Budget-Preflight je Batch, private Abschlussberichte
  und zunächst optionale **E-Mail per SMTP mit Benutzername/Passwort**.
  Webhooks, Messenger-/Push-Adapter, OAuth für Mail und provider-spezifische
  Guthabenabfragen bleiben Folgearbeit.

## Umsetzungsreife und Änderungen seit dem ursprünglichen Plan

**Die Implementierung kann mit dem Runtime-Gate beginnen.** Die fachliche
Richtung ist festgelegt; automatische Modellläufe werden aber erst nach belegter
Writer-Koordination, Recovery und endlichen Ausführungsgrenzen freigegeben.
Die folgenden bereits implementierten Bausteine wiederverwenden, nicht neu bauen:

- #173, #176–#179: Git-/Quellenprüfung, verpflichtende Fehlerrecords, private
  Entwürfe und codebasierter Publisher. Worker besitzen keine Shell-/Edit-Rechte;
  Index/Log, Commit und Erfolgsrecord entstehen durch geprüfte Veröffentlichung.
- #179: Kernel-Lock für Publikation, atomare synchronisierte Journal-/Reportwrites
  und persistierte Publikationsabsicht mit `state`/`resume`. `next_batch` priorisiert
  unterbrochene Publikationen auch bei bereits `current` gewordenen Quellen.
- #181: Kontextüberschreitungen warnen, statt Lesen oder Veröffentlichung allein
  aufgrund geschätzter Bytes/Toolaufrufe abzubrechen. Vollständige Quellenlektüre
  und echte Modellgrenzen bleiben maßgeblich; das neue Verbrauchsbudget ist separat.
- #182: OpenChamber zeigt private Laufberichte über einen authentifizierten,
  read-only Mount von `incoming/ingest-journal/runs`; keine neue Berichtsroute nötig.
- #183: Verifizierte Paperless-Generationswechsel vor `prepare` sind kein falscher
  Blocker, wenn Identität, Revision und Wiki-Ziel gleich bleiben.
- #186: Die Hauptsession liefert zusätzlich eine kompakte Übersicht jeder
  bearbeiteten Datei aus effektivem Journal; vollständige Details bleiben privat.
- #184: Das ausgelieferte OpenCode-Image pinnt **2.0.25**.

Noch offen: instanzweite Writer-Zulassung einschließlich manueller Wartung,
Controller/Timer, idempotenter Dispatch, persistente Retry-/Budgetsteuerung und
SMTP-Outbox. Ein Publikationslock ersetzt keine Sperre über den Sitzungsbaum.
Die bestehende Realmodell-/UI-Akzeptanz ist nicht durch diesen Plan nachgewiesen;
die dokumentierten offenen Abnahmen bleiben bestehen.

## Bestehende Integrationspunkte

| Bereich | Ausgangspunkt |
| --- | --- |
| Bulk-Kommando und Rechte | `config/opencode.json`, `config/routing.md` |
| Orchestrator/Worker | `config/skills/wiki-ingest-orchestrator/SKILL.md`, `config/skills/wiki-ingest/SKILL.md` |
| Modellfreier Status | `config/tools/wiki_ingest_status_core.mjs`, `config/plugins/wiki-ingest-status.js` |
| Batches, Ergebnisse, Berichte | `config/tools/wiki_ingest_journal_core.mjs`, `config/plugins/wiki-ingest-journal.js` |
| Private Entwürfe und verifizierte Publikation | `config/tools/wiki_ingest_publication_core.mjs`, `config/tools/wiki_ingest_transaction_core.mjs`, `config/plugins/wiki-ingest-transaction.js` |
| Kernel-Locks und atomare Persistenz | `config/tools/wiki_ingest_storage.mjs` |
| Runtime und Start | `opencode/Dockerfile`, `opencode/entrypoint.sh`, `compose.yaml` |
| Initialisierung und Konfiguration | `config/init.sh`, `.env.example`, `docs/configuration.md` |
| Betriebsvertrag | `docs/ingest-reports.md`, `docs/data-layout.md`, `docs/architecture.md`, `README.md` |
| Bestehende Akzeptanz und private Berichtsanzeige | `docs/ingest-stabilization-acceptance.md`, `tests/acceptance/ingest_stabilization.py`, `docs/chat.md`, `tests/integration/chat.mjs` |

Das Journal prüft Erfolgsrecords und `verify_batch` gegen Publisherbelege,
Git-Commit, geänderte Pfade und Quellenrevision/-bytes. Publikation und
Recordupdates verwenden bestehende Kernel-Locks; `run.json`, Records und
Berichte werden atomar mit Datei-/Verzeichnissync geschrieben. Die verbleibende
Lücke ist die gemeinsame Zulassung von Läufen und aller anderen Wiki-Writer,
nicht das erneute Implementieren dieser Prüfungen. Eine bereits aktuelle Quelle
mit ausstehender Publikationsbestätigung über den vorhandenen Receipt wiederaufnehmen;
ohne verifizierbare Belege weder neu committen noch Berichtsinhalte erfinden.

## Architekturentscheidung

**Bevorzugt: periodischer, modellfreier Controller als OpenCode-V2-Plugin.**
Er benutzt den vorhandenen Statusscanner und startet bei tatsächlichem Bedarf
das vorhandene Kommando in einer frischen Orchestrator-Sitzung. Ein Timer nach
Abschluss des vorigen Durchlaufs koalesziert Änderungen aller Quellenanbieter;
kein Cronjob in OpenChamber und kein zusätzlicher Provider-Hook sind nötig.
Nur für die SMTP-Zustellung einen kleinen, separat aktivierten Compose-Dienst
vorsehen, damit SMTP-Secrets außerhalb des Modell-Backends bleiben (Abschnitt 7).
Er hat keinen Scheduler-/Ingestauftrag. Quellenereignisse dürfen später die Latenz
verkürzen, bleiben aber außerhalb dieses ersten Umfangs.

Das Plugin arbeitet ausschließlich für `/knowledge/wiki`, nicht für beliebige
Locations. Der Timer startet erst nach abgeschlossenem Plugin-Setup; Setup darf
nicht auf die eigene Location-Aktivierung warten. Cleanup stoppt Timer und
Subscriptions. Ein gemeinsamer Controller verhindert doppelte Timer/Dispatches
bei Reload oder mehrfacher Location-Aktivierung.

### Runtime-Nachweise und verbindliches erstes Gate

Zur Planung geprüft:

- Das Repository pinnt OpenCode **2.0.25**. Die jeweils lokale CLI-Version
  gesondert erfassen; lokale Ergebnisse sind kein Nachweis für das Image.
- Die V2-Dokumentation beschreibt Plugin-Setup/Cleanup, Timer, dauerhaften
  Plugin-Speicher, Session- und Permission-Hooks sowie das Ausführen von Commands.
- Die veröffentlichte V2-OpenAPI beschreibt Session-Erstellung mit Location,
  Agent und Modell, `parentID`, Command-Ausführung, aktive Sessions und Interrupt.
  HTTP-Command-Aufrufe verwenden `name` und `text`; Plugin-Aufrufe haben einen
  eigenen dokumentierten Vertrag. Keine V1-Endpunkte übernehmen.
- `session.active` erfasst nur aktive Foreground-Ausführungen. Ein fehlender
  Eintrag beweist nicht, dass keine Hintergrund-Shell mehr läuft.
- Ein eingebauter persistenter Cron-Scheduler ist durch diese Nachweise nicht
  belegt; der eigene Timer braucht deshalb eine durable Wiederanlaufstrategie.

Vor der Umsetzung des Controllers einen isolierten **modellfreien Runtime-Test
im gepinnten Image** schreiben und ausführen:

1. Plugin laden, Location eindeutig bestimmen und Command-/Agent-Erkennung prüfen.
2. Session-API, Modellwahl, Eltern-/Kind-Zuordnung und aktive/beschäftigte Zustände
   prüfen; Dispatch-Zulassung nicht mit abgeschlossener Verarbeitung verwechseln.
3. Mit einem synthetischen Plugin-Command ohne Modell die Admission-/Idle-
   Semantik, Permission-Denial und Tool-Hooks einschließlich Kind-Sessions prüfen.
4. Background-Shell, Interrupt und tatsächliches Ende der Ausführung prüfen;
   zusätzlich direkte Shell-/PTY-/Dateisystem-API-Pfade inventarisieren. Ein
   Modell-Tool-Hook allein schützt nicht alle manuellen Writer-Pfade.
5. Timer-Cleanup, Reload, Neustart und Boot ohne geöffneten Chat prüfen.
6. Verfügbarkeit und Semantik gemeldeter Token-/Kosten-Usage, Modellpreise und
   Vorab-Guards über Orchestrator, Kinder, Nebenrequests und Provider-Retries
   prüfen. Unbekannte Usage nicht als Nullverbrauch behandeln.

Location-Plugins müssen auch ohne Browserzugriff geladen werden. Bevorzugt den
bestehenden authentifizierten Healthcheck um einen modellfreien Location-Aufruf
für `/knowledge/wiki` ergänzen, sofern der Runtime-Test dessen Ladeverhalten
bestätigt. Andernfalls einen kleinen überwachten Warmup im Startpfad vorsehen.

Falls die erforderlichen Guards oder Session-Lifecycle-Signale nicht zuverlässig
unterstützt werden, Architekturentscheidung neu treffen und im Plan dokumentieren;
nicht durch Skill-Anweisungen allein eine technische Sperre behaupten.
Die aktuelle V2-Dokumentation beschreibt Hooks, ist aber kein Versionsnachweis
für 2.0.25. Die dort dokumentierte Command-Admission ist insbesondere keine
Exactly-once-Ausführungs- oder Abschlussgarantie.

Referenzen:

- <https://opencode.ai/v2/docs/build/plugins>
- <https://opencode.ai/v2/docs/api>
- <https://opencode.ai/v2/openapi.json>

## Geplanter Betriebsvertrag

### 1. Explizites Opt-in und Modellwahl

Neue Variablen im Namespace `WIKI_AUTO_INGEST_*` vorsehen:

- `ENABLED=0` standardmäßig; nur explizites Aktivieren erlaubt Modellstarts.
- `MODEL` als erforderliche `provider/model`-Auswahl beim Aktivieren; kein
  stiller Rückgriff auf das zuletzt im Chat benutzte Modell.
- `INTERVAL_SECONDS` als positiver Prüf-/Ruheabstand, zunächst z. B. 300 Sekunden.
- Endliche `MAX_ATTEMPTS`, `BACKOFF_SECONDS`, `MAX_RUN_SECONDS` und
  `MAX_MODEL_REQUESTS`; konkrete Defaults nach dem Runtime-Gate festlegen.
- Konfigurierbare kumulative `BUDGET_TOKENS` und/oder `BUDGET_COST_USD`,
  `BUDGET_PERIOD` (`day`/`month`), `BUDGET_TIMEZONE` (standardmäßig UTC) sowie
  eine dokumentierte Sicherheitsreserve für Vorabschätzungen. Namen und
  Defaults beim Runtime-Gate abschließend festlegen; diese Limits ergänzen
  die immer endlichen Ausführungsgrenzen, ersetzen sie nicht.
- E-Mail separat standardmäßig deaktiviert; SMTP-Ziel, Absender, Empfänger,
  Ereignisauswahl und Detailfreigabe ausschließlich aus vertrauenswürdiger
  Betreiberkonfiguration beziehen. Benutzername/Passwort nur dem Versanddienst
  bereitstellen, nicht OpenCode. Kein Modellwerkzeug zum beliebigen Versenden
  oder Auswählen eines Empfängers anbieten. Konfiguration siehe Abschnitt 7.

Werte strikt prüfen. Fehlerhafte Aktivierung oder nicht verfügbares Modell führt
zu sichtbarem `blocked`/`failed`, nicht zu einem Fallback-Modell. Das gewählte
Modell muss auch für Worker gelten. Vorhandene Provider-Anmeldungen werden intern
von OpenCode verwendet; der Controller liest oder kopiert keine Credentials.
Die Kontextparameter aus #152 gelten unverändert. Automatische Läufe müssen
endliche Batch-/Request-Limits haben, auch wenn manuelle Läufe unbegrenzt sein dürfen.

### 2. Modellfreier Preflight und koaleszierter Dispatch

1. Aktuellen Quellen-/Wiki-Status scannen, bevor eine Session angelegt wird.
2. Ohne `new`/`outdated` und ohne gespeicherte Publikationsabsicht: kein
   Modelllauf; `idle`, sofern keine offenen Berichts-/Recovery-Probleme vorliegen.
   Ausstehende Receipts zuerst modellfrei erkennen und über vorhandenes
   `state`/`resume` abgleichen, auch wenn alle Quellen bereits `current` sind.
3. Bei `invalid`/`conflict`: global stoppen und private Diagnosen bereitstellen.
4. Fremde aktive Sitzungen, laufende Shells sowie Git-Index und Working Tree
   prüfen. Bei ungeklärtem Zustand oder fremden Änderungen nicht starten.
5. Den nächsten Batch modellfrei planen und Kontext-, Verbrauchs-/Kostenbudget
   einschließlich Reserve prüfen (siehe Abschnitt 5). Reicht es nicht, ohne
   Modellstart pausieren. Planung dafür ohne Hochzählen dispatchter Batches
   ermöglichen; ein abgelehnter Preflight verbraucht keine Batch-Zulassung.
   Ein überschrittenes weiches Kontextziel allein ist kein Ablehnungsgrund.
6. Writer-Sperre atomar übernehmen; unter der Sperre Status/Git und Budget erneut
   prüfen, Reservierung und Startabsicht atomar speichern.
7. Session-/Run-Zuordnung dauerhaft speichern, dann genau einmal
   `/ingest-new` zulassen. Admission-ID/Idempotenz gegen die Runtime prüfen.
8. Während eines Laufs keine weiteren Starts; neue Quellen werden durch die
   nächste frische Batchplanung oder den nächsten Prüfzyklus erfasst.
9. Vor jedem weiteren Batch Budget und Reserve erneut prüfen. Den vorhandenen
   Orchestrator-/Journal-Pfad verwenden; kein zweiter Batchplanner.
10. Abschluss anhand von Status, Journal und Git prüfen, nicht anhand der letzten
    Modellantwort oder eines erfolgreichen HTTP-Requests.

### 3. Koordination aller Wiki-Writer

Eine gemeinsame Writer-Sperre gilt für automatische Läufe und manuelle
Schreibsitzungen, insbesondere `/ingest`, `/ingest-new`, `/lint`,
`/analysis-save` und schreibende `build`-Aufträge.
Sie ergänzt den vorhandenen kurzen Publikationslock. Lock-Reihenfolge festlegen
und testen; keine verschachtelte erneute Übernahme desselben Kernel-Locks.

- Eigentümer ist ein Sitzungsbaum mit eindeutiger Besitzkennung; Orchestrator
  und sequenzielle Kinder teilen sie, andere Sitzungen nicht. Vererbung anhand
  der tatsächlichen Session-Hierarchie prüfen, nicht anhand von Prompttext.
- Über Permission-Hooks bestehende Schreibrechte bei fehlender/fremder
  Sperre einschränken; nie ein konfiguriertes `deny` aufheben. Tool-Hooks sichern
  die Besitzprüfung und laufende Operationen ab. Alle Shell-Aufrufe konservativ
  als potenziell schreibend behandeln, keine unsichere Command-String-Erkennung.
- Auch mutierende Journaloperationen serialisieren; lesende Status-/Berichts-
  Tools bleiben nutzbar. Ein manueller Bulk-Lauf darf nicht den aktiven
  automatischen Journal-Lauf übernehmen und gleichzeitig bearbeiten.
- Aktuelle Transaction-Tools einschließlich `prepare`, `stage`, `publish`,
  `resume` und `rollback` an den Sessionbesitz binden; ihre heutigen Argumente
  allein belegen keine Zulassung. Direkte API-Schreibpfade technisch abdecken
  oder bei aktiviertem Auto-Ingest zuverlässig sperren. Bleibt ein unterstützter
  Writer-Pfad ungesichert, darf die Automatik nicht freigegeben werden. Keine
  Sicherheit allein aus UI-Hinweisen oder dem read-only Frontend-Mount ableiten.
- Ist eine manuelle Sitzung zuerst aktiv, wartet die Automatik modellfrei.
  Ist die Automatik zuerst aktiv, erhält ein manueller Writer eine klare
  Busy-Meldung vor der Mutation; keine unbegrenzten Warteschleifen im Modell.
- Sperre erst freigeben, wenn Writer-Kinder und zugehörige Shells beendet sind.
  Ablauf einer Lease oder ein Idle-Ereignis allein berechtigt nicht zur Übernahme.
  Alte Besitzkennungen dürfen nach einer Übernahme keine neuen Writes ausführen.
- Bei Unterbrechung mit Dirty Tree blockieren. Niemals automatisch `stash`,
  `reset`, `checkout`, fremde Dateien löschen oder fremde Änderungen committen.
- Benötigt der bestehende Fehlervertrag eine Entscheidung, als
  `operator_action_required` pausieren und benachrichtigen, statt unbeaufsichtigt
  auf `question` zu warten. Auto-Ingest ist keine Zustimmung zu Wartung,
  `rollback`, `skip_blocked`, Neuerfassung oder Wiederholung unbekannter Writes.
  Nur die bestehende einmalige korrekte Eingabekorrektur bzw. verifizierte
  Receipt-Wiederaufnahme ist ohne neue Reparaturentscheidung zulässig.

Externe Host-Editoren und zusätzliche OpenCode-Server sind keine technisch
abgesicherten Teilnehmer dieser Plugin-Sperre. Opt-in setzt eine einzelne
zuständige Runtime voraus; Host-Änderungen während eines Laufs sind zu vermeiden.
Git-/Revisionsprüfungen erkennen Abweichungen, bieten aber keine transaktionale
Absicherung gegen jede externe Race Condition. Diese Grenze ausdrücklich nennen.

### 4. Dauerhafter Zustand, Recovery und Berichtsprüfung

Controllerzustand und Koordination bevorzugt unter dem vorhandenen privaten
`incoming/ingest-journal/automation/` ablegen (`0700`, Dateien `0600`). Keine
Montierung des gesamten SQLite-State-Stores in OpenCode. Bestehende
`withIngestLock`-/`writeIngestFile`-Primitive wiederverwenden und ihre Abdeckung
auf Laufzulassung und Controllerzustand erweitern; keine zweite Persistenzschicht.

Persistieren: Zustand, Besitz-/Session-/Run-IDs, Startabsicht, letzte verifizierte
Erfolge, ausstehende Arbeit/Blocker, Retry-Zähler, nächster zulässiger Versuch,
verbrauchte Limits und Berichtspfade. Budgetperioden, Usage-Nachweise,
Reservierungen und Versandzustand ebenfalls dauerhaft speichern. Keine
Quelltexte in Controller-Logs. Budgetpausen halten den Journal-Lauf offen und
stellen den Zwischenbericht bereit; sie sind kein endgültiger Quellenblocker.

Nach Neustart vor jedem Dispatch:

- Alten Sessionbaum, laufende Operationen, Git und aktuellen Status abgleichen.
  Eine noch aktive Arbeit beobachten, nicht neu dispatchen.
- Bereits aktuelle, belegte Revisionen nicht erneut verarbeiten. Journaldaten
  gegen existierenden Commit, Git-Historie, geänderte Seiten und revisionsbezogene
  Provenienz prüfen. Ungültige Resultate nicht als Erfolg zählen.
- Crash zwischen Commit und Journalrecord: Commit nicht duplizieren. Fehlende
  Bestätigung aus dem bereits persistierten vollständigen Publikationspayload
  verifiziert nachführen. Fehlt dieser Beleg, einen Berichtsblocker ausweisen;
  interpretative Details nicht aus Git erfinden.
- Dirty Tree, kaputtes Journal oder unklare Besitzverhältnisse führen zu einem
  konkreten Recovery-Blocker mit Operator-Anleitung.
- Rollover nur an Batchgrenzen, in frischer Orchestrator-Sitzung und innerhalb
  der gespeicherten Limits fortsetzen. Fortschritt aus verifizierten Resultaten,
  nicht aus Kontext oder alten Status-Offsets übernehmen.

### 5. Budget-Preflight, endliche Wiederholungen und Kosten

Drei unterschiedliche Budgets ausdrücklich auseinanderhalten:

| Budget | Prüfung / Grenze |
| --- | --- |
| Arbeitskontext je Worker | Bestehendes weiches Schätzziel aus #152/#181; genau ein frischer Worker pro Quelle, gezielte paginierte Reads und Ausgabe-Headroom. Warnungen sind kein hartes Limit; echte Modellfenster bleiben begrenzt |
| Lokales Verbrauchs-/Kostenlimit | Dauerhafter Zähler für automatische Ingest-Arbeit je konfigurierter Tages-/Monatsperiode; geschätzten nächsten Batch plus Reserve gegen Restbudget prüfen |
| Provider-Guthaben / Abo-Kontingent | Ohne passende Provider-Schnittstelle unbekannt; keine allgemeine Abfrage verbleibender Tokens voraussetzen |

Das lokale Limit gilt zunächst für automatische Ingest-Sitzungsbäume dieser
Instanz, nicht für sämtliche manuelle Chats oder die gesamte Providerrechnung.
Zusätzlichen Verbrauch anderer Anwendungen kann der Controller nicht erkennen.
Tokenverbrauch bezeichnet abgerechnete Input-/Output-Tokens einschließlich
Caching-/Reasoning-Anteilen gemäß nachgewiesener Provider-Usage; Semantik und
Vermeidung von Doppelzählung dokumentieren. Kosten separat anhand bekannter
Preise/Usage ausweisen, niemals Kontexttokens mit Rechnungsbetrag gleichsetzen.

**Vor jedem Batch:** Bedarf aus gemessenen Größen, Arbeits- und Ausgabeannahmen,
erwarteten Wiederholungen des Kontexts und ggf. verifizierten bisherigen
Verbrauchswerten vergleichbarer Batches schätzen. Schätzung mit Unsicherheit und
Reserve ausweisen; reine Quelldateigröße unterschätzt mehrere Modellrequests.
Bei bekannten Preisen auch Kosten schätzen, ohne Cache-Hits als sicher anzunehmen.
Der bestehende Planner weist bereits genau eine Quelle pro Batch zu; keine neue
Mehrquellen-Batchlogik einführen. Passt diese Quelle einschließlich Reserve nicht
ins lokale Verbrauchsbudget, pausieren statt sie halb zu schreiben. Eine Quelle
nicht wegen des weichen Kontextziels allein ablehnen oder still kürzen.

Reservierung und tatsächliche Usage atomar und idempotent nachführen, auch für
Kinder, Nebenrequests und Retries. Nach jedem gemeldeten Verbrauch den nächsten
Batch neu beurteilen. Eine offene Reservierung nach Crash oder verzögerte/fehlende
Usage bleibt ungeklärt und reduziert verfügbares Budget; sie wird weder durch
Neustart noch durch automatische Wiederaufnahme gelöscht. Sind Preise/Usage für
ein aktiviertes Limit nicht zuverlässig ermittelbar, mit `budget_unknown`
blockieren und konkrete Abhilfe nennen, nicht Nullkosten behaupten.

Budget-Prüfungen an Batchgrenzen sind keine harten Token-/Kostenlimits während
einer Quelle. Unterstützte Request-Guards und endliche Request-/Zeitgrenzen
ergänzen sie; bei Überschreitung keine neue Quelle bzw. keinen neuen Batch
beginnen. Unterbrechungen weiterhin nach dem Dirty-Tree-/Recovery-Vertrag behandeln.

Budgetpausen als `blocked` mit separatem Grund darstellen:

- `local_budget_exhausted`: lokales Limit bzw. Reserve reicht nicht; Verbrauch,
  Schätzung, Restarbeit und nächste Periodengrenze berichten.
- `provider_quota_exhausted`: eindeutige Kontingentmeldung des Providers;
  nicht jede Rate-Limit-Antwort (429) als aufgebrauchtes Guthaben deuten.
- `budget_unknown`: erforderlicher Verbrauchs-/Preisnachweis fehlt. Der bloß
  unbekannte Provider-Kontostand blockiert dagegen nicht, solange keine
  Provider-Guthabenprüfung zugesichert ist und lokale Guards funktionieren.

Periodengrenzen in konfigurierter Zeitzone berechnen und persistieren; nur das
periodische Budget wird dann erneuert. Ungeklärte Reservierungen sicher zuordnen,
nicht still wegsetzen. Bei lokalem Budget nach Reset frischen Preflight ausführen;
kein Budgetoverride durch gewöhnliches Resume. Providerkontingent nur nach
nachgewiesenem Reset oder expliziter Bestätigung der Abhilfe erneut versuchen.
Budgeterhöhung ist eine bewusste Betreiberentscheidung. Die Retry-Sperren
unveränderter Quellen-/Recovery-Blocker bleiben davon unabhängig erhalten.

**Endliche Wiederholungen:**

- Transiente Runtime-/Providerfehler mit begrenzten Versuchen und gedeckeltem
  Backoff behandeln; Zähler vor Dispatch persistieren.
- Unveränderte Quellenblocker, globale Konflikte, Dirty Tree und fehlende
  Berichtsnachweise dürfen keinen neuen Modelllauf pro Poll auslösen.
- Retry-Sperren auf die betroffene Quellenidentität/-revision bzw. den konkreten
  globalen Blocker beziehen. Eine neue, unabhängige Quelle darf nicht die
  Retry-Zähler eines alten Blockers zurücksetzen.
- Nach Ausschöpfen der Limits nur durch explizites Rearm/Resume oder eine
  relevante Änderung erneut versuchen; Neustart/Reload setzt nichts zurück.
- Provider-Retries per unterstütztem Retry-Hook begrenzen. Modellrequests,
  einschließlich Worker und Nebenrequests, soweit möglich vor Dispatch zählen.
  Zeitlimit führt zu kontrollierter Unterbrechung; Sperre nicht vor tatsächlichem
  Ende freigeben. Blocker dürfen nicht durch Modell-Retry-Schleifen teuer werden.
- Kein garantierter harter Euro-/Dollar-Spend-Cap: Kontextbudget ist eine
  Schätzung, Usage kann verzögert sein, laufende Requests können bereits Kosten
  verursachen. Tatsächlich erzwingbare Grenzen und restliche Risiken dokumentieren.

### 6. Sichtbarkeit und manuelle Bedienung

`idle`, `running`, `blocked`, `failed`, letzte verifizierte erfolgreiche
Verarbeitung, nächste Prüfung/Retry, offene Quellen und Berichtspfade anbieten.
Zusätzlich Verbrauchsperiode, gemeldete Usage, Kostenschätzung, Reservierungen,
Restbudget, Pausengrund und ggf. Resetzeit anzeigen; Messwerte, Schätzungen und
Unbekanntes klar trennen. Versandstatus nicht mit Ingeststatus vermischen.
Einen begrenzt ausgebenden Status-/Resume-Zugang vorsehen; Status ist lesend,
Rearm ausdrücklich und nur ohne aktiven Writer. Keine neue öffentliche Route.
Direkter Operator-Zugriff auf private Dateien bleibt möglich.

Per-Source-Berichte weiter aus dem Journal erstellen, Tatsachen gegen Git
prüfen und Details vollständig privat speichern. Logs enthalten nur erlaubte
Statuscodes, Zähler und opake Laufkennungen; keine Pfade mit privaten Werten,
Quellinhalte, Prompts, rohe Providerfehler oder Credentials.

Deaktivierung verhindert neue Starts. Verhalten eines bereits laufenden Auftrags
explizit festlegen: regulär zu Ende laufen lassen; separater Stop unterbricht
kontrolliert, ohne uncommittete Änderungen zu verwerfen. `/ingest-new` bleibt
manuell nutzbar und respektiert dieselbe Writer-Koordination.

### 7. Modellfreie Benachrichtigungen und optionale SMTP-E-Mail

Ergebnis und vollständiger Bericht bleiben privat zugänglich, vorzugsweise über
die Ingest-Sitzung in OpenChamber und den bestehenden Journalzugang. Keine
ungeprüfte native Browser-/Push-Unterstützung zusichern. Nachrichten entstehen
deterministisch aus Controllerzustand und verifiziertem Journal, ohne zweiten
Modelllauf zur Zusammenfassung oder zum Versand.

Ereignisse:

- **Erfolgreicher Lauf:** Zahl neu eingelesener/aktualisierter Quellen, Restarbeit,
  verfügbare Verbrauchswerte sowie privater Sitzungs-/Berichtsverweis.
- **Teilabschluss:** verifizierte bearbeitete Quellen, offene Arbeit und Blocker.
- **Budgetpause:** Limittyp, bekannter Verbrauch, Restarbeit und Reset-/Resume-
  Hinweis. Prognostizierte Budgetknappheit nicht als gemessene Erschöpfung melden.
- **Fehler/Konflikt:** konkreter Handlungsbedarf ohne rohe Providerfehler.
- Leere Prüfläufe erzeugen keine Nachricht; derselbe unveränderte Blocker wird
  nicht bei jedem Poll erneut gemeldet. Einen Teilabschluss mit Budgetpause
  möglichst in einer Nachricht zusammenfassen.

Der erste und einzige Zustelladapter in #157 ist **SMTP mit Benutzername und
Passwort**, ohne OAuth oder externen Automationsdienst. App-Passwörter sind möglich,
sofern der Mailanbieter SMTP AUTH zulässt; Anbieter ohne diese Möglichkeit brauchen
einen geeigneten SMTP-Relay. Webhook, Matrix und ntfy/Gotify bleiben Folgearbeit.

#### Konfiguration und Secret-Grenze

Geplante Variablen (Präfix jeweils `WIKI_AUTO_INGEST_EMAIL_`):

| Variable | Vertrag |
| --- | --- |
| `ENABLED` | Default `0`; zusätzlich Compose-Profil `notifications` aktivieren |
| `SMTP_HOST`, `SMTP_PORT` | Festes Betreiberziel; Port `587` bei STARTTLS bzw. `465` bei implizitem TLS |
| `SMTP_TLS_MODE` | `starttls` als Default oder `tls`; niemals Klartext-Fallback |
| `SMTP_USERNAME_FILE`, `SMTP_PASSWORD_FILE` | Read-only Secret-Dateien ausschließlich im Versanddienst, z. B. `/run/secrets/smtp_username` und `/run/secrets/smtp_password` |
| `FROM`, `TO` | Fester Absender und zunächst genau ein Empfänger; niemals aus Quellen oder Journal ableiten |
| `EVENTS` | Default Erfolg, Teilabschluss, Budgetpause und Fehler; keine leeren Polls |
| `INCLUDE_DETAILS` | Default `0`; separate bewusste Freigabe begrenzter privater Details |
| `TIMEOUT_SECONDS`, `MAX_ATTEMPTS`, `BACKOFF_SECONDS` | Endliche Versandgrenzen; konkrete Defaults bei Umsetzung festlegen |

Aktivierte E-Mail mit fehlenden/ungültigen Werten zeigt einen separaten
Konfigurations-/Versandfehler. Ohne `ENABLED=1` weder Nachrichten erzeugen noch
SMTP verbinden. Bei fehlendem Versanddienst ausstehende Zustellung sichtbar machen,
ohne Ingest zu wiederholen. Ingest darf unabhängig von E-Mail aktiviert werden.

Den kleinen Versanddienst vorzugsweise mit Python-Standardbibliothek
(`smtplib`, `email.message.EmailMessage`, validierendem TLS-Kontext) umsetzen.
Damit braucht die OpenCode-Runtime weder SMTP-Paket noch SMTP-Zugangsdaten.
Er erhält nur dedizierte private Outbox und eigenen Versandzustand, keine Wiki-,
Quellen-, Journal-Preparations-, OpenCode-State- oder Docker-Socket-Mounts.
Eigene Egress-Netzanbindung für SMTP, keine Mitgliedschaft im Backend-/Proxy-Netz
und keine veröffentlichten Ports; ohne Modell-/Backend-Zugangsdaten betreiben.
Request-Outbox read-only montieren; Acknowledgements/Versandzustand separat
schreibbar, damit der Sender keine Controller-/Ingestdaten ändern kann. OpenCode
liest Acknowledgements read-only. SMTP-Secrets weder im Backend-/Frontend-
Environment noch in modelllesbaren Dateien ablegen. Kein SMTP-Secret beim
gewöhnlichen Default-off-Start verlangen; optionales Compose-Profil dafür testen.

Extern standardmäßig nur Ereignistyp, Zähler, Budgetangaben und opake Kennung
senden. Quellnamen/-pfade, private URLs, Commitdetails und Inhaltszusammenfassungen
sind potenziell privat und bleiben standardmäßig ausgeschlossen. Erst ausdrückliche
Detailfreigabe erlaubt ausgewählte Angaben aus verifizierten Journalrecords;
Inhalte begrenzen, nicht erfinden und niemals rohe Quellen/Sessionexports versenden.
Ein privater Dateipfad ist kein extern erreichbarer Berichtlink; keine neue
öffentliche Berichtsroute zur Zustellung einführen.

Outboxschema versionieren und stabile Ereignis-ID vorsehen. Requests unter
`automation/outbox/`, Senderzustand/Acknowledgements unter
`automation/delivery/` privat (`0700`/`0600`) speichern. Nur feste, validierte
Ereignisfelder, keine frei formulierbaren SMTP-Befehle, Empfänger, Header oder
Anhangspfade akzeptieren; symlinkfreie begrenzte Reads und atomare Publikation.
Outboxgröße, Detailbytes und Aufbewahrung endlich begrenzen: nur bestätigte
vom SMTP-Server angenommene Einträge nach dokumentierter Frist entfernen; bei voller Outbox
sichtbaren Versandblocker melden, keine stillen Verluste oder Modell-Retries.
Eventerzeugung idempotent anhand Run-/Pausenkennung und Zustandswechsel, auch nach
Neustart. Der Sender persistiert Versuche vor Versand; Timeout, gedeckeltes
Backoff und endliche Versuche gelten unabhängig vom Ingestbudget. Nach
Ausschöpfung sichtbarer Versandfehler und separates manuelles Wiederholen.

Mail deterministisch als begrenzten Plaintext aus erlaubten Ereignisfeldern
erzeugen, ohne Modellaufruf, aktive HTML-Inhalte oder vollständige Reportanhänge.
Betreff enthält nur Status und opake Kennung, keine Quellnamen. Standardmäßig
auf den Berichtzugang im privaten Chat verweisen, keinen Dateipfad als anklickbare
Mail-URL erfinden. Details nur nach Opt-in und aus verifiziertem Journal, sicher
als Text serialisieren; CR/LF in Konfigurationsheadern/Adressen ablehnen.

Ein Versandfehler **startet niemals einen neuen Ingest**, setzt keine Budgets
zurück und macht keinen verifizierten Commit rückgängig. Netzwerkzustellung ist
keine Exactly-once-Garantie: bei verlorener Bestätigung nach SMTP-Annahme kann
ein Retry doppelt ankommen. Eine stabile `Message-ID` aus der Ereignis-ID beibehalten;
Mailserver/-clients müssen deswegen nicht deduplizieren. SMTP-Annahme bedeutet
nicht belegte Zustellung ins Empfängerpostfach; entsprechend als `accepted`
ausweisen, nicht als garantiert gelesen/zugestellt.

TLS mit Hostname-/Zertifikatsprüfung zwingend; bei STARTTLS erst nach erfolgreichem
Upgrade authentifizieren. Fehlendes STARTTLS, Zertifikats- und Authentifizierungs-
fehler ohne Klartext-Fallback beenden. Permanente SMTP-Fehler blockieren Versand;
transiente Fehler nur begrenzt wiederholen. Kein SMTP-Debuglogging: Credentials,
Server-/Empfängerwerte, Nachrichtentext und rohe SMTP-Antworten nicht loggen.
Test- und Statusausgaben enthalten ausschließlich erlaubte Fehlercodes/Zähler.
SMTP-TLS schützt den Transport zum Relay, ist keine Ende-zu-Ende-Verschlüsselung;
auch freigegebene Details verlassen die private Installation und können im
Postfach/Relay gespeichert werden. Diese Datenschutzgrenze dokumentieren.

## Umsetzung in überprüfbaren Schritten

1. **Runtime-Gate:** modellfreien Probe-/Integrationstest für 2.0.25 ergänzen;
   belegte API-/Hook-Verträge und sichere Boot-Aktivierung festhalten.
2. **Koordination und Journal:** modellfreien Core für Writer-Besitz bauen;
   vorhandene Locks, atomare Writes, Publisher-/Git-Verifikation und Recovery
   wiederverwenden. Laufzulassung und Transaction-Tools an Sessionbesitz binden.
   Permission-/Tool-/API-Guards erst mit deterministischen Contention-Tests anbinden.
3. **Controller:** neuen Core, voraussichtlich
   `config/tools/wiki_auto_ingest_core.mjs`, mit injizierbarer Uhr, Runtime,
   Scanner und Persistenz; Pluginadapter
   `config/plugins/wiki-auto-ingest.js` für Timer, Dispatch und Lifecycle.
4. **Recovery und Grenzen:** Restart-Reconciliation, idempotente Admission,
   sourcebezogene Retry-Sperren, Rollover und Abbruchgrenzen integrieren. Budget-
   Preflight je Batch, persistente Usage/Reservierungen und Periodenreset ergänzen.
5. **Benachrichtigungen:** modellfreie Ereigniserzeugung, private Durable Outbox
   und isolierten SMTP-Sender mit Username-/Passwort-Secrets, verpflichtendem TLS,
   Datenschutz-Defaults und begrenztem Versand implementieren. Kein Webhook,
   OAuth-, Push-/Messenger- oder Guthabenadapter im ersten Umfang.
6. **Deployment und Bedienung:** `.env.example`, `compose.yaml`, Initialisierung
   und gegebenenfalls Startpfad ergänzen. Sender-Image, optionales
   `notifications`-Profil, Secret-/Outbox-Mounts und CI-/Image-Publikation ergänzen.
   Status-/Resume-Zugang anbinden;
   Routing und relevante Skills an den Koordinationsvertrag anpassen, ohne
   Kontext-/Provenienzregeln aus #152 abzuschwächen.
7. **Dokumentation:** `docs/auto-ingest.md` für Opt-in, Modellvoraussetzungen,
   Grenzen, Berichtsabruf, Disable/Stop, Busy-/Dirty-/Conflict-Fälle und Resume;
   Budgetarten/-periode/-scope, Schätzunsicherheit, unbekanntes Providerkontingent,
   SMTP-/Secret-Konfiguration, TLS-/Mailanbieter-Voraussetzungen, Mail-Datenschutz,
   Outbox-Aufbewahrung und Versand-Recovery erklären.
   README sowie Konfigurations-, Architektur-, Datenlayout- und Reportdocs verlinken.
8. **Gesamtvalidierung:** CI-Anbindung neuer Tests, Docker-Integration und
   vorhandenen opt-in Realmodell-Harness um automatische Starts erweitern. SMTP
   mit synthetischem lokalen Testserver prüfen, nicht mit echten Zugangsdaten oder
   echten Empfängern. Vor dem Implementierungs-PR alle README-Checks sowie neue
   Sender-/Runtime-Tests ausführen und Evidenz dokumentieren.

## Testplan / Abdeckung der Akzeptanzkriterien

Normale Tests sind deterministisch, synthetisch und modellfrei. Neue
`tests/test_wiki_auto_ingest.mjs` und Koordinationstests mit Fake-Uhr, Fake-Runtime,
temporärem Git-Wiki und Testjournal vorsehen. Bestehende Journal-/Status- und
Orchestrationstests erweitern; relevante statische Verträge in Python prüfen.

| Fall | Erwartung |
| --- | --- |
| Default aus / falsche Konfiguration | Kein Timer-Dispatch, keine Modellkosten; Fehler sichtbar |
| Keine pending Quellen und keine Recovery-Receipts | Keine Session und kein Modellrequest |
| Wiederholte Polls / schnelle Publikationen | Ein koaleszierter Lauf, weitere Arbeit frisch planen |
| Zwei gleichzeitige Starts / Plugin-Reload | Nur ein Sperreigentümer und ein Dispatch |
| Manual zuerst / Automatic zuerst | Fremder Writer wartet bzw. wird vor Mutation abgewiesen |
| Direkte Shell-/PTY-/Dateisystem-API und Wartung | Alle unterstützten Writer-Pfade geschützt oder technisch gesperrt; kein Opt-in bei Guard-Lücken |
| Transaction-Tool mit fremdem Run/Receipt | Kein Writer-Zugriff über frei gelieferte IDs |
| Sequenzielle Kind-Sessions | Besitz korrekt geerbt, kein zweiter paralleler Writer |
| Background-Shell / Interrupt / Lease-Ablauf | Kein verfrühter Sperrwechsel |
| Fremde staged/unstaged/untracked Änderungen | Blockieren; Dateien und Index unverändert |
| Stale Scan / geänderte Revision vor Write | Frisch prüfen, veraltete Arbeit nicht schreiben |
| `invalid` / `conflict`, auch während eines Laufs | Globaler Stop, Diagnosen und Restarbeit sichtbar |
| Unveränderter Quellenblocker / neue unabhängige Quelle | Kein Kostenloop; alte Retry-Sperre bleibt bestehen |
| Transiente Fehler / Retry-Limit / Restart | Endliches Backoff, persistente Versuchszähler |
| Request-/Zeit-/Batchlimit und Rollover | Keine grenzenlose Fortsetzung; sichere Wiederaufnahme |
| Budget reicht / einzelne Quelle zu teuer | Genau eine Quelle vorab reservieren oder ohne Modell-/Write-Start pausieren |
| Weiches Kontextziel überschritten | Warnung, keine alte Byte-/Aufruf-Abbruchgrenze; voller Quellen-/Publikationsvertrag |
| Wiederholter Kontext, Worker, Nebenrequests, Retries | Gemeldete Usage korrekt und idempotent zählen; Schätzung nicht als Messwert ausgeben |
| Fehlende Preise/Usage / verzögerte Abrechnung / Crash | Aktiviertes Limit nicht mit Nullverbrauch umgehen; ungeklärte Reservierung bleibt wirksam |
| Periodenreset, Zeitzone, Restart und Resume | Budget konsistent erneuern; gewöhnliches Resume umgeht Limit nicht; Quellenblocker bleiben bestehen |
| Unbekanntes Providerguthaben / Quota-Fehler / temporäres 429 | Unterschiedliche Diagnosen; keine falsche Erschöpfungsmeldung oder unendliche Retry-Schleife |
| Restart vor/nach Admission, vor/nach Commit/Record | Keine Doppelzulassung/-commits; fehlende Details blockieren |
| Keine pending Quelle, aber unbestätigter Publisher-Receipt | Bestehendes `state`/`resume` priorisieren, keine zweite Extraktion |
| Verifizierter Paperless-Generationswechsel | Gleiche Identität/Revision/Ziel akzeptieren; tatsächlichen Pfad erhalten |
| Gefälschter/fehlender Commit oder Pfadnachweis | Kein verifizierter Erfolg |
| Bounded-context-/Oversized-Fälle aus #152 | Budget und vollständiger Bericht bleiben erhalten |
| Disable, Stop, Resume und Berichtszugriff | Definierter Lifecycle; private Speicherrechte eingehalten |
| Fehler verlangt Bestätigung / unbeantwortete Frage | `operator_action_required`, kein automatisches Rollback/Skip/Reingest oder endloses Warten |
| Hauptsession und vollständiger Bericht | Aktueller per-file Vertrag bleibt erhalten; read-only Chatbericht, Preparations/Automation nicht im Frontend |
| Logs und Rechte | Keine privaten Inhalte/Secrets; Quellen read-only; keine Rechteausweitung |
| Erfolg, Teilabschluss, Budgetpause und leerer Poll | Verifizierte modellfreie Nachricht je relevantem Zustandswechsel; leere Polls still |
| E-Mail aus / Profil aus / fehlerhafte SMTP-Konfiguration | Default-off ohne benötigte Mailsecrets; separater sichtbarer Versandfehler, kein Ingest-Retry |
| Defaultmail / explizite Details | Keine Quellnamen/-inhalte/private Links ohne Freigabe; begrenzter Plaintext, keine Reportanhänge |
| SMTP-TLS / STARTTLS fehlt / ungültiges Zertifikat / AUTH-Fehler | Kein Klartext-Fallback, AUTH erst nach TLS; ausschließlich synthetische Credentials |
| SMTP-4xx/5xx, Timeout, Neustart und verlorene Bestätigung | Endliche persistente Retries, stabile Message-ID; mögliche Duplikate benennen, niemals erneuter Ingest |
| Manipulierte Quelle / Header-Injection / Outbox-Symlink | Nur fester Absender/Empfänger; Schema-/Größenprüfung und sichere Reads |
| SMTP-Secrets und Sender-Mounts | Backend/Frontend ohne Mailcredentials; Sender ohne Wiki-/Quellen-/Runtimezugriff |
| Volle Outbox / Aufbewahrung / Sender fehlt | Begrenzter Speicher, sichtbare Versandblocker; keine Löschung unbestätigter Ereignisse |

Docker-Integration prüft zusätzlich die ausgelieferte Plugin-Erkennung,
Boot-Aktivierung ohne Chat, Guard-Wirkung, Neustartpersistenz und Default-off;
zusätzlich Senderisolation und lokale synthetische SMTP-Annahme/-Fehler.
Ein explizit kostenpflichtiger Realmodell-Smoke-Test bleibt außerhalb normaler
CI: isolierte Installation, ausschließlich synthetische Quellen, bewusst
gewähltes Modell und endliche Limits. `new → current → outdated → current`,
ein Commit je Revision, vollständige private Berichte, Busy-Verhalten und
Wiederanlauf überprüfen. Öffentlich nur inhaltsfreie Ergebnisse und Runtime/
Modell sowie beobachtete Limits festhalten; keine Sessionexports veröffentlichen.

Validierung vor PR gemäß `README.md`: `scripts/lint.sh`,
`scripts/test-python.sh`, alle dort genannten Node-Tests plus neue Tests,
beide Chat-Image-Builds, neuen Sender-Image-Build, `tests/integration/chat.sh`,
`tests/integration/run.sh` und neue modellfreie Auto-Ingest-/SMTP-Integration.
Verhaltensänderungen, Sicherheitsfolgen,
fehlende Migrationen und Testevidenz im späteren PR beschreiben.

## Offene Implementierungsentscheidungen

- Tatsächliche Permission-/Tool-Hook-Abdeckung aller erlaubten Writer-Pfade
  sowie direkte API-Writer und Background-/Kind-Lifecycle in 2.0.25.
- Sichere, modellfreie Location-Aktivierung beim Serverstart.
- Idempotente Command-Admission und Requestzählung über Sessionbäume.
- Nachgewiesene Usage-/Preissemantik, Budgetreservierung über Crashgrenzen und
  Guards vor Requests, ohne einen harten Provider-Spend-Cap zu behaupten.
- Konkrete Limit-Defaults und die kleinste sichere Status-/Rearm-Oberfläche.
- Outboxschema, SMTP-Sender-Image und Aufbewahrungsgrenzen.
  Kanal und Anmeldung sind entschieden: SMTP mit Username/Passwort und TLS;
  weitere Zustelladapter und Provider-Guthabenabfragen bleiben außerhalb des Umfangs.

Runtime-Fragen zuerst im gepinnten Image belegen; Sender-/Outboxdetails danach
mit modellfreien Tests festlegen. Dies sind Implementierungsgates, keine
behaupteten Runtime-Garantien.
