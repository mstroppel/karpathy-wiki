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
5. Writer-Sperre atomar übernehmen; unter der Sperre Status/Git erneut prüfen.
6. Startabsicht und Session-/Run-Zuordnung dauerhaft speichern, dann genau einmal
   `/ingest-new` zulassen. Admission-ID/Idempotenz gegen die Runtime prüfen.
7. Während eines Laufs keine weiteren Starts; neue Quellen werden durch die
   nächste frische Batchplanung oder den nächsten Prüfzyklus erfasst.
8. Abschluss anhand von Status, Journal und Git prüfen, nicht anhand der letzten
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
verbrauchte Limits und Berichtspfade. Keine Quelltexte in Controller-Logs.

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

### 5. Endliche Wiederholungen und Kosten

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
   sourcebezogene Retry-Sperren, Rollover und Abbruchgrenzen integrieren.
5. **Deployment und Bedienung:** `.env.example`, `compose.yaml`, Initialisierung
   und gegebenenfalls Startpfad ergänzen. Status-/Resume-Zugang anbinden;
   Routing und relevante Skills an den Koordinationsvertrag anpassen, ohne
   Kontext-/Provenienzregeln aus #152 abzuschwächen.
6. **Dokumentation:** `docs/auto-ingest.md` für Opt-in, Modellvoraussetzungen,
   Grenzen, Berichtsabruf, Disable/Stop, Busy-/Dirty-/Conflict-Fälle und Resume;
   README sowie Konfigurations-, Architektur-, Datenlayout- und Reportdocs verlinken.
7. **Gesamtvalidierung:** CI-Anbindung neuer Tests, Docker-Integration und
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
| Restart vor/nach Admission, vor/nach Commit/Record | Keine Doppelzulassung/-commits; fehlende Details blockieren |
| Gefälschter/fehlender Commit oder Pfadnachweis | Kein verifizierter Erfolg |
| Bounded-context-/Oversized-Fälle aus #152 | Budget und vollständiger Bericht bleiben erhalten |
| Disable, Stop, Resume und Berichtszugriff | Definierter Lifecycle; private Speicherrechte eingehalten |
| Logs und Rechte | Keine privaten Inhalte/Secrets; Quellen read-only; keine Rechteausweitung |

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
- Konkrete Limit-Defaults und die kleinste sichere Status-/Rearm-Oberfläche.

Diese Punkte sind Implementierungsgates, keine behaupteten Runtime-Garantien.
