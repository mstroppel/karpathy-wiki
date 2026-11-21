# Time-series analysis in wiki chat

`timeseries` is opt-in. The importer uses Home Assistant's [history REST API](https://developers.home-assistant.io/docs/api/rest/#get-apihistoryperiodtimestamp)
to archive Recorder history for explicitly selected entities. Queries do not
contact Home Assistant. Owlet is an example of a Home Assistant entity, not
a separate integration.

## Enable or change the selection

1. Make sure Home Assistant's History integration is available and Recorder
   retains the selected entities. Create a long-lived access token for a Home
   Assistant user that can read those entities. Save it as a private file
   readable by the installation's `PUID`/`PGID`. Use HTTPS unless the importer
   reaches Home Assistant over an independently protected local connection.
2. Set `HOME_ASSISTANT_URL` to the instance base URL and, if using a non-default
   path, set `HOME_ASSISTANT_TOKEN_FILE` in `.env`. The default token file is
   `./secrets/home-assistant-token`. Add `timeseries` to `COMPOSE_PROFILES` in
   `.env` (this also enables the chat tool). Run `./karpathy-wiki.sh run --rm init`
   to create the private directories.
3. Create `${DATA_ROOT}/timeseries/config/entities.json` as a JSON list of actual
   Home Assistant `entity_id` strings. For example, if your entities have these
   IDs:

   ```json
   ["sensor.owlet_sleep_state", "sensor.room_temperature"]
   ```

4. Run `./karpathy-wiki.sh up -d`. The importer starts immediately and runs the
   same job every 15 minutes, without overlapping runs. Each entity starts with
   the history still retained by Recorder; later runs read from one hour before
   its last successfully imported cutoff, including after restarts. There is no
   interpolation or downsampling on import. Recorder history purged before it
   was imported cannot be recovered.

The importer re-reads the list each cycle. Removing an ID stops future imports
for it; the existing archive remains queryable. To delete an archived series and
its import position, remove its ID (an empty list is `[]`), then run:

```sh
./karpathy-wiki.sh exec timeseries-import delete sensor.owlet_sleep_state --confirm
```

Deletion is an administrator operation, not a chat action. It does not remove
data from Home Assistant, chat transcripts, previously saved analyses, model
providers or backups. Re-selecting a deleted ID imports what Recorder still
has. Use a new archive when switching to an unrelated Home Assistant instance:
the stable IDs are `ha:<entity_id>` within one Wiki installation.

## What the chat can query

- `series` has stable IDs, source and entity IDs, display names, descriptions,
  units, series kind, timestamp meaning and the last successful import cutoff
  (`imported_through_utc`/`imported_through_us`). Names use `friendly_name` or
  the ID; descriptions use `description` or remain empty. Series with a unit or
  `state_class` are treated as measurements; other series are categorical.
- `samples` contains Recorder records at their original resolution. UTC
  `timestamp_utc` and integer `timestamp_us` represent HA `last_updated`
  (a Recorder update, not necessarily the device's sample time). Measurement
  values are numbers; categorical values are text. `raw_value` retains the
  exact HA state string, and `unit` is the unit recorded on that sample, if any.
  `unknown`, `unavailable`, missing and invalid values have a distinct `quality`
  and NULL `value`; a measured zero remains zero.
- A successful cutoff is **not** a guarantee of coverage. An explicit unknown
  or unavailable update ends coverage; it does not count towards deep sleep or
  a shared interval. The next timestamp ends the interval even if its records
  disagree. The API cannot distinguish a silent Recorder outage from a stable
  state. Measurements held between records require an explicit maximum hold
  time and a compatible unit; no interpolation is assumed. State intervals
  without a known next boundary are not counted. The chat should report its
  timezone, analysis window, method and coverage separately from the import
  cutoff.

Ask a question in ordinary language and use follow-ups in the same chat. For
example, ask about the temperature distribution during deep sleep, then restrict
it to nights with at least two hours of shared coverage. The same SELECT tool
supports both queries, including joins, CTEs and window functions. It returns
bounded results with a truncation flag; use aggregate queries or page through
raw results. Use `/analysis-save` only when you want an analysis saved to the
wiki. Reader mode permits queries but still disables saving.

## Data and safety boundaries

The importer is the only service with the HA credential and write access to the
SQLite archive under `${DATA_ROOT}/timeseries/archive`. The SQL worker mounts
the archive read-only and has no network, credentials or wiki mount. OpenCode
has access only to its Unix socket, with read-only queries limited to `series`
and `samples`. The archive is not published via SilverBullet or `raw-files`.
Launcher backups include the archive and token; they omit the transient socket.

**Only enable this profile if your configured chat models may receive these data.**
Tool results can include raw individual values, timestamps and names. They can
be stored in the private chat transcript and sent to a hosted model provider
without another confirmation. Nothing is added to the wiki by a query alone.
