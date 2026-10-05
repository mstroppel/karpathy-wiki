# Implementierungsplan: #157 – automatisches `/ingest-new`

Issue: <https://github.com/mstroppel/karpathy-wiki/issues/157>

Branch: `feat/157-auto-ingest-new`

Planungsstand: 2026-10-05, ausgehend von `c79f621` auf `main`.

## Ziel und Grenzen

Neue und veraltete, bereits bereinigte Quellen werden nach ausdrücklichem
Opt-in automatisch mit dem bestehenden `/ingest-new` verarbeitet. Ohne Opt-in
bleibt das heutige Verhalten bestehen. Dieser Branch enthält zunächst nur die
Planung; eine Implementierung ist noch nicht enthalten.

- #152 ist abgeschlossen und mit `fc32ada` bereits enthalten: kontextbegrenzte
  Batches, sequenzielle Worker, privates Journal und dateibasierter Bericht.
- Keine zweite Ingest-Implementierung, keine Parallelisierung von Wiki-Writern
  und keine Abhängigkeit vom zurückgestellten transaktionalen Publisher #113.
- Quellen bleiben unveränderliche, nicht vertrauenswürdige Daten. Keine
  zusätzlichen Modellrechte, Provider-Zugriffe oder Offenlegung von Zugangsdaten.
- Keine Migrationen, Kompatibilitätsaliase oder automatischen Datenverschiebungen.
- `revoked` und `orphaned` werden weiterhin berichtet, nicht automatisch bereinigt.
- Zusätzlich vereinbart: Budget-Preflight je Batch, private Abschlussberichte
  und ein optionaler generischer Benachrichtigungs-Webhook. Provider-spezifische
  Guthabenabfragen sowie eigene E-Mail-/Messenger-/Push-Adapter bleiben Folgearbeit.

## Bestehende Integrationspunkte

| Bereich | Ausgangspunkt |
| --- | --- |
| Bulk-Kommando und Rechte | `config/opencode.json`, `config/routing.md` |
| Orchestrator/Worker | `config/skills/wiki-ingest-orchestrator/SKILL.md`, `config/skills/wiki-ingest/SKILL.md` |
| Modellfreier Status | `config/tools/wiki_ingest_status_core.mjs`, `config/plugins/wiki-ingest-status.js` |
| Batches, Ergebnisse, Berichte | `config/tools/wiki_ingest_journal_core.mjs`, `config/plugins/wiki-ingest-journal.js` |
| Runtime und Start | `opencode/Dockerfile`, `opencode/entrypoint.sh`, `compose.yaml` |
| Initialisierung und Konfiguration | `config/init.sh`, `.env.example`, `docs/configuration.md` |
| Betriebsvertrag | `docs/ingest-reports.md`, `docs/data-layout.md`, `docs/architecture.md`, `README.md` |

Das Journal prüft heute die Datensatzform; die Batchplanung gleicht Ergebnisse
mit frischem Status ab. Das ist noch keine unabhängige Prüfung von Git-Commit,
geänderten Pfaden und berichteter Revision. `run.json` wird direkt geschrieben;
die gemeinsame Laufaufnahme und Journaländerungen haben noch keine technische
Writer-Sperre. Diese Lücken müssen vor automatischer Wiederaufnahme geschlossen
werden. Eine bereits aktuelle Quelle ohne vollständigen Ergebnisdatensatz darf
nicht erneut committet oder mit erfundenen Berichtsinhalten ergänzt werden.

## Architekturentscheidung

**Bevorzugt: periodischer, modellfreier Controller als OpenCode-V2-Plugin.**
Er benutzt den vorhandenen Statusscanner und startet bei tatsächlichem Bedarf
das vorhandene Kommando in einer frischen Orchestrator-Sitzung. Ein Timer nach
Abschluss des vorigen Durchlaufs koalesziert Änderungen aller Quellenanbieter;
kein Cronjob in OpenChamber, kein zusätzlicher Provider-Hook und kein neuer
Compose-Service sind nötig. Ereignisse dürfen später die Latenz verkürzen,
bleiben aber außerhalb dieses ersten Umfangs.

Das Plugin arbeitet ausschließlich für `/knowledge/wiki`, nicht für beliebige
Locations. Der Timer startet erst nach abgeschlossenem Plugin-Setup; Setup darf
nicht auf die eigene Location-Aktivierung warten. Cleanup stoppt Timer und
Subscriptions. Ein gemeinsamer Controller verhindert doppelte Timer/Dispatches
bei Reload oder mehrfacher Location-Aktivierung.

### Runtime-Nachweise und verbindliches erstes Gate

Zur Planung geprüft:

- Das Repository pinnt OpenCode **2.0.23**; die lokale CLI meldet **2.0.22**.
  Lokale Ergebnisse sind daher kein Nachweis für das ausgelieferte Image.
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
4. Background-Shell, Interrupt und tatsächliches Ende der Ausführung prüfen.
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
- Webhook separat standardmäßig deaktiviert; Ziel, Ereignisauswahl, optionale
  Authentifizierung über Secret-Datei und Detailfreigabe ausschließlich aus
  vertrauenswürdiger Betreiberkonfiguration beziehen. Kein Modellwerkzeug zum
  beliebigen Versenden von Nachrichten oder Auswählen eines Empfängers anbieten.

Werte strikt prüfen. Fehlerhafte Aktivierung oder nicht verfügbares Modell führt
zu sichtbarem `blocked`/`failed`, nicht zu einem Fallback-Modell. Das gewählte
Modell muss auch für Worker gelten. Vorhandene Provider-Anmeldungen werden intern
von OpenCode verwendet; der Controller liest oder kopiert keine Credentials.
Die Kontextparameter aus #152 gelten unverändert. Automatische Läufe müssen
endliche Batch-/Request-Limits haben, auch wenn manuelle Läufe unbegrenzt sein dürfen.

### 2. Modellfreier Preflight und koaleszierter Dispatch

1. Aktuellen Quellen-/Wiki-Status scannen, bevor eine Session angelegt wird.
2. Ohne `new`/`outdated`: kein Modelllauf; `idle`, sofern keine offenen
   Berichts-/Recovery-Probleme vorliegen.
3. Bei `invalid`/`conflict`: global stoppen und private Diagnosen bereitstellen.
4. Fremde aktive Sitzungen, laufende Shells sowie Git-Index und Working Tree
   prüfen. Bei ungeklärtem Zustand oder fremden Änderungen nicht starten.
5. Den nächsten Batch modellfrei planen und Kontext-, Verbrauchs-/Kostenbudget
   einschließlich Reserve prüfen (siehe Abschnitt 5). Reicht es nicht, ohne
   Modellstart pausieren. Planung dafür ohne Hochzählen dispatchter Batches
   ermöglichen; ein abgelehnter Preflight verbraucht keine Batch-Zulassung.
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
- Ist eine manuelle Sitzung zuerst aktiv, wartet die Automatik modellfrei.
  Ist die Automatik zuerst aktiv, erhält ein manueller Writer eine klare
  Busy-Meldung vor der Mutation; keine unbegrenzten Warteschleifen im Modell.
- Sperre erst freigeben, wenn Writer-Kinder und zugehörige Shells beendet sind.
  Ablauf einer Lease oder ein Idle-Ereignis allein berechtigt nicht zur Übernahme.
  Alte Besitzkennungen dürfen nach einer Übernahme keine neuen Writes ausführen.
- Bei Unterbrechung mit Dirty Tree blockieren. Niemals automatisch `stash`,
  `reset`, `checkout`, fremde Dateien löschen oder fremde Änderungen committen.

Externe Host-Editoren und zusätzliche OpenCode-Server sind keine technisch
abgesicherten Teilnehmer dieser Plugin-Sperre. Opt-in setzt eine einzelne
zuständige Runtime voraus; Host-Änderungen während eines Laufs sind zu vermeiden.
Git-/Revisionsprüfungen erkennen Abweichungen, bieten aber keine transaktionale
Absicherung gegen jede externe Race Condition. Diese Grenze ausdrücklich nennen.

### 4. Dauerhafter Zustand, Recovery und Berichtsprüfung

Controllerzustand und Koordination bevorzugt unter dem vorhandenen privaten
`incoming/ingest-journal/automation/` ablegen (`0700`, Dateien `0600`). Keine
Montierung des gesamten SQLite-State-Stores in OpenCode. Sperrübernahme atomar,
Zustandsdateien per temporärer Datei und Rename schreiben; Journaländerungen
gegen gleichzeitige Writes und unterbrochene Schreibvorgänge absichern.

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
  interpretative Details als Berichtsblocker ausweisen, nicht aus Git erfinden.
- Dirty Tree, kaputtes Journal oder unklare Besitzverhältnisse führen zu einem
  konkreten Recovery-Blocker mit Operator-Anleitung.
- Rollover nur an Batchgrenzen, in frischer Orchestrator-Sitzung und innerhalb
  der gespeicherten Limits fortsetzen. Fortschritt aus verifizierten Resultaten,
  nicht aus Kontext oder alten Status-Offsets übernehmen.

### 5. Budget-Preflight, endliche Wiederholungen und Kosten

Drei unterschiedliche Budgets ausdrücklich auseinanderhalten:

| Budget | Prüfung / Grenze |
| --- | --- |
| Kontextfenster je Worker | Bestehende Schätzung aus #152 für Quelle, Wiki-Seiten und Arbeitsaufwand; Headroom für Ausgabe reservieren, Batch verkleinern oder Oversized-Vertrag anwenden |
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
Passt der Batch nicht, ihn innerhalb des #152-Planners verkleinern. Passt selbst
die nächste Quelle mit Reserve nicht, pausieren statt sie halb zu schreiben.

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

### 7. Modellfreie Benachrichtigungen und optionaler Webhook

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

Der erste Zustelladapter ist ein **optional aktivierter generischer Webhook**,
z. B. für einen eigenen Empfänger, Home Assistant oder n8n. E-Mail, Matrix,
ntfy/Gotify und Provider-Guthabenadapter sind dokumentierte Erweiterungsoptionen,
nicht zusätzliche direkte Integrationen in #157.

Extern standardmäßig nur Ereignistyp, Zähler, Budgetangaben und opake Kennung
senden. Quellnamen/-pfade, private URLs, Commitdetails und Inhaltszusammenfassungen
sind potenziell privat und bleiben standardmäßig ausgeschlossen. Erst ausdrückliche
Detailfreigabe erlaubt ausgewählte Angaben aus verifizierten Journalrecords;
Inhalte begrenzen, nicht erfinden und niemals rohe Quellen/Sessionexports versenden.
Ein privater Dateipfad ist kein extern erreichbarer Berichtlink; keine neue
öffentliche Berichtsroute zur Zustellung einführen.

Payload versionieren und stabile Ereignis-ID vorsehen. Durable Outbox im privaten
Controllerverzeichnis speichert Versandstatus, nächste Versuche und gewählte
Payload; Detailpayloads ebenfalls nur privat. Eventerzeugung idempotent anhand
Run-/Pausenkennung und Zustandswechsel, auch nach Neustart. Versand außerhalb der
Writer-Sperre mit Timeout, gedeckeltem Backoff und endlichen Versuchen; nach
Ausschöpfung sichtbarer Versandfehler und separates manuelles Wiederholen.

Ein Versandfehler **startet niemals einen neuen Ingest**, setzt keine Budgets
zurück und macht keinen verifizierten Commit rückgängig. Netzwerkzustellung ist
keine Exactly-once-Garantie: bei verlorener Bestätigung kann ein Retry doppelt
ankommen; Empfänger kann anhand der stabilen Ereignis-ID deduplizieren.

Ziel-URL fest aus Betreiberkonfiguration, HTTPS standardmäßig, keine Redirects
zu anderen Zielen; HTTP für lokale Dienste nur mit bewusstem Opt-in. Secret-Datei
nicht dem Modell offenlegen. URLs, Authorization, Payloads und Antwortbodies nicht
loggen. Begrenzte Antwortgröße/Timeouts sowie verlässliche Abbruch- und
Datenschutzregeln testen; Quellen können weder Empfänger noch Header ändern.

## Umsetzung in überprüfbaren Schritten

1. **Runtime-Gate:** modellfreien Probe-/Integrationstest für 2.0.23 ergänzen;
   belegte API-/Hook-Verträge und sichere Boot-Aktivierung festhalten.
2. **Koordination und Journal:** modellfreien Core für Writer-Besitz und atomaren
   Controllerzustand bauen; Journal-Writes absichern und Git-Resultate verifizieren.
   Permission-/Tool-Guards erst mit deterministischen Contention-Tests anbinden.
3. **Controller:** neuen Core, voraussichtlich
   `config/tools/wiki_auto_ingest_core.mjs`, mit injizierbarer Uhr, Runtime,
   Scanner und Persistenz; Pluginadapter
   `config/plugins/wiki-auto-ingest.js` für Timer, Dispatch und Lifecycle.
4. **Recovery und Grenzen:** Restart-Reconciliation, idempotente Admission,
   sourcebezogene Retry-Sperren, Rollover und Abbruchgrenzen integrieren. Budget-
   Preflight je Batch, persistente Usage/Reservierungen und Periodenreset ergänzen.
5. **Benachrichtigungen:** modellfreie Ereigniserzeugung, private Durable Outbox
   und optionalen Webhookadapter mit Datenschutz-Defaults und begrenztem Versand
   implementieren; keine zusätzlichen Push-/Messenger- oder Guthabenadapter.
6. **Deployment und Bedienung:** `.env.example`, `compose.yaml`, Initialisierung
   und gegebenenfalls Startpfad ergänzen. Status-/Resume-Zugang anbinden;
   Routing und relevante Skills an den Koordinationsvertrag anpassen, ohne
   Kontext-/Provenienzregeln aus #152 abzuschwächen.
7. **Dokumentation:** `docs/auto-ingest.md` für Opt-in, Modellvoraussetzungen,
   Grenzen, Berichtsabruf, Disable/Stop, Busy-/Dirty-/Conflict-Fälle und Resume;
   Budgetarten/-periode/-scope, Schätzunsicherheit, unbekanntes Providerkontingent,
   Webhook-/Secret-Konfiguration, Payload-Datenschutz und Versand-Recovery erklären.
   README sowie Konfigurations-, Architektur-, Datenlayout- und Reportdocs verlinken.
8. **Gesamtvalidierung:** CI-Anbindung neuer Tests, Docker-Integration und
   ausdrücklich aktivierbaren Realmodell-Smoke-Test ergänzen; vor einem später
   beauftragten PR alle README-Checks ausführen und Evidenz dokumentieren.

## Testplan / Abdeckung der Akzeptanzkriterien

Normale Tests sind deterministisch, synthetisch und modellfrei. Neue
`tests/test_wiki_auto_ingest.mjs` und Koordinationstests mit Fake-Uhr, Fake-Runtime,
temporärem Git-Wiki und Testjournal vorsehen. Bestehende Journal-/Status- und
Orchestrationstests erweitern; relevante statische Verträge in Python prüfen.

| Fall | Erwartung |
| --- | --- |
| Default aus / falsche Konfiguration | Kein Timer-Dispatch, keine Modellkosten; Fehler sichtbar |
| Keine pending Quellen | Keine Session und kein Modellrequest |
| Wiederholte Polls / schnelle Publikationen | Ein koaleszierter Lauf, weitere Arbeit frisch planen |
| Zwei gleichzeitige Starts / Plugin-Reload | Nur ein Sperreigentümer und ein Dispatch |
| Manual zuerst / Automatic zuerst | Fremder Writer wartet bzw. wird vor Mutation abgewiesen |
| Sequenzielle Kind-Sessions | Besitz korrekt geerbt, kein zweiter paralleler Writer |
| Background-Shell / Interrupt / Lease-Ablauf | Kein verfrühter Sperrwechsel |
| Fremde staged/unstaged/untracked Änderungen | Blockieren; Dateien und Index unverändert |
| Stale Scan / geänderte Revision vor Write | Frisch prüfen, veraltete Arbeit nicht schreiben |
| `invalid` / `conflict`, auch während eines Laufs | Globaler Stop, Diagnosen und Restarbeit sichtbar |
| Unveränderter Quellenblocker / neue unabhängige Quelle | Kein Kostenloop; alte Retry-Sperre bleibt bestehen |
| Transiente Fehler / Retry-Limit / Restart | Endliches Backoff, persistente Versuchszähler |
| Request-/Zeit-/Batchlimit und Rollover | Keine grenzenlose Fortsetzung; sichere Wiederaufnahme |
| Budget reicht / Batch zu groß / einzelne Quelle zu teuer | Vorab reservieren, Batch verkleinern oder ohne Modell-/Write-Start pausieren |
| Wiederholter Kontext, Worker, Nebenrequests, Retries | Gemeldete Usage korrekt und idempotent zählen; Schätzung nicht als Messwert ausgeben |
| Fehlende Preise/Usage / verzögerte Abrechnung / Crash | Aktiviertes Limit nicht mit Nullverbrauch umgehen; ungeklärte Reservierung bleibt wirksam |
| Periodenreset, Zeitzone, Restart und Resume | Budget konsistent erneuern; gewöhnliches Resume umgeht Limit nicht; Quellenblocker bleiben bestehen |
| Unbekanntes Providerguthaben / Quota-Fehler / temporäres 429 | Unterschiedliche Diagnosen; keine falsche Erschöpfungsmeldung oder unendliche Retry-Schleife |
| Restart vor/nach Admission, vor/nach Commit/Record | Keine Doppelzulassung/-commits; fehlende Details blockieren |
| Gefälschter/fehlender Commit oder Pfadnachweis | Kein verifizierter Erfolg |
| Bounded-context-/Oversized-Fälle aus #152 | Budget und vollständiger Bericht bleiben erhalten |
| Disable, Stop, Resume und Berichtszugriff | Definierter Lifecycle; private Speicherrechte eingehalten |
| Logs und Rechte | Keine privaten Inhalte/Secrets; Quellen read-only; keine Rechteausweitung |
| Erfolg, Teilabschluss, Budgetpause und leerer Poll | Verifizierte modellfreie Nachricht je relevantem Zustandswechsel; leere Polls still |
| Webhook aus / Defaultpayload / explizite Details | Ohne Opt-in kein Versand; standardmäßig keine Quellnamen/-inhalte/private Links |
| Webhook-Timeout/Fehler, Neustart und verlorene Bestätigung | Durable Outbox, begrenzte Retries und stabile Ereignis-ID; niemals erneuter Ingest |
| Manipulierte Quelle / Redirect / Webhook-Secret | Nur konfiguriertes Ziel; kein frei wählbarer Empfänger, Credential- oder Payload-Logging |

Docker-Integration prüft zusätzlich die ausgelieferte Plugin-Erkennung,
Boot-Aktivierung ohne Chat, Guard-Wirkung, Neustartpersistenz und Default-off.
Ein explizit kostenpflichtiger Realmodell-Smoke-Test bleibt außerhalb normaler
CI: isolierte Installation, ausschließlich synthetische Quellen, bewusst
gewähltes Modell und endliche Limits. `new → current → outdated → current`,
ein Commit je Revision, vollständige private Berichte, Busy-Verhalten und
Wiederanlauf überprüfen. Öffentlich nur inhaltsfreie Ergebnisse und Runtime/
Modell sowie beobachtete Limits festhalten; keine Sessionexports veröffentlichen.

Validierung vor PR gemäß `README.md`: `scripts/lint.sh`,
`scripts/test-python.sh`, alle dort genannten Node-Tests plus neue Tests,
beide Chat-Image-Builds, `tests/integration/chat.sh` und
`tests/integration/run.sh`. Verhaltensänderungen, Sicherheitsfolgen,
fehlende Migrationen und Testevidenz im späteren PR beschreiben.

## Noch durch das Runtime-Gate zu entscheiden

- Tatsächliche Permission-/Tool-Hook-Abdeckung aller erlaubten Writer-Pfade
  sowie Background-/Kind-Lifecycle in 2.0.23.
- Sichere, modellfreie Location-Aktivierung beim Serverstart.
- Idempotente Command-Admission und Requestzählung über Sessionbäume.
- Nachgewiesene Usage-/Preissemantik, Budgetreservierung über Crashgrenzen und
  Guards vor Requests, ohne einen harten Provider-Spend-Cap zu behaupten.
- Konkrete Limit-Defaults und die kleinste sichere Status-/Rearm-Oberfläche.
- Konkrete Webhook-Konfiguration und Payload-Version; direkte Zustelladapter
  und Provider-Guthabenabfragen bleiben außerhalb des ersten Umfangs.

Diese Punkte sind Implementierungsgates, keine behaupteten Runtime-Garantien.
