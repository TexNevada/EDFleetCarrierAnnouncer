# Elite Dangerous journal behavior and webhook review

Reviewed 2026-10-09, including the proposed earlier-`FSDJump` lookup and
offboard `CarrierLocation` destination broadcasts.

**The seven findings below have been fixed and their reproductions now pass
as normal regression tests.** System-information enrichment still works for
the supplied CCGL journal. Startup reconstructs state silently, reconnect
snapshots reconcile position, and uncertain offline schedules are discarded
without claiming success or cancellation.

| Finding | Implemented resolution |
| --- | --- |
| R1: historical startup notifications | Replay returns no notification payloads by default; final reconstructed position is saved independently of delivery. Unread live records alone enter the delivery queue. There is no historical backfill. |
| R2: snapshot falsely completes a jump | Only an observed matching arrival can complete a schedule. `Location` snapshots clear uncertain schedules and reconcile position; numeric carrier identity takes precedence over station text. |
| R3: offline gaps retain misleading schedules | `FileHeader` for a new session, `Commander`/`LoadGame`, `Shutdown`, and `Resurrect` invalidate pending schedules. New positions report `location_changed`. An origin position never implies cancellation. Live completion checks destination address/body and scheduled departure time when available. |
| R4: partial cancellation is lost | Both startup and live readers commit byte offsets only through newline-terminated records, including UTF-8 split across writes. Old files and intervening parts are drained before switching. |
| R5: continuation context is discarded | Matching session names, sequential part numbers, and `Continued.Part` preserve docking and earlier FSD visits. Startup and warm tailing rebuild context from available preceding session parts. Missing continuity invalidates pending schedules. |
| R6: dedup expires before replay does | Accepted notification identities are retained beyond the raw-observation TTL. Journal filename/byte-offset identities identify observations; request-based jump identities correlate completion records. Startup is silent, including on upgrade. |
| R7: HTTP failures are marked delivered | An atomic persistent outbox precedes HTTP POST. Only 2xx acceptance enters delivery dedup. Failures retry with exponential backoff; 429 honors `Retry-After` or JSON `retry_after`. Messages sharing a webhook respect queued retry order. Cancelled/completed/invalidated/expired unsent schedules are removed. Pending messages follow updated webhook settings; removed routes stop delivery. |

The historical descriptions below record the original bugs. File/line
references in that review described the pre-fix implementation, not the
current code. The remaining limits are documented at the end.

## Online sources and what they establish

Sources were opened and read online on 2026-10-09. The Read the Docs pages
are a community-readable reproduction of the journal documentation, not a
guarantee that every 2026 client behavior is covered. Their introduction
contains a version 37 changelog describing May 2023 changes, and explicitly
warns that examples can be out of date.

| Source | Relevant documented behavior |
| --- | --- |
| [Journal file format](https://elite-journal.readthedocs.io/en/latest/File%20Format.html) | Line-delimited JSON, one object per line; UTC/GMT ISO-8601 timestamps; `Journal.<datestamp>.<part>.log`; large journals can continue into another part, each with a header. |
| [Startup: Commander and LoadGame](https://elite-journal.readthedocs.io/en/latest/Startup.html#loadgame) | `Commander` identifies the commander and FID. `LoadGame` is written “at startup, when loading from main menu into game.” A load boundary matters even if the announcer process remains running. |
| [Travel: Location](https://elite-journal.readthedocs.io/en/latest/Travel.html#location) | Written “at startup, or when being resurrected at a station.” It describes current location and docking state; it is not exclusively a jump-arrival event. |
| [Travel: FSDJump](https://elite-journal.readthedocs.io/en/latest/Travel.html#fsdjump) | Written when jumping between star systems; contains system identity and system details. `Body` is the arrival star, not the carrier's selected orbit body. |
| [Fleet carriers: CarrierJump](https://elite-journal.readthedocs.io/en/latest/Fleet%20Carriers.html#carrierjump) | Written “if the player is online and docked at a fleet carrier when the carrier jumps.” A player who was offline should not be assumed to have a historical `CarrierJump` for every carrier movement. |
| [Fleet carriers: request and cancellation](https://elite-journal.readthedocs.io/en/latest/Fleet%20Carriers.html#carrierjumprequest) | `CarrierJumpRequest` is written when the player requests the jump, “not the jump itself.” It includes destination/body and `DepartureTime`. `CarrierJumpCancelled` is logged when a jump is cancelled and identifies the carrier. |
| [Other events: Continued and Shutdown](https://elite-journal.readthedocs.io/en/latest/Other%20Events.html#continued) | `Continued` closes one journal and starts another part. `Shutdown` is written on a clean shutdown; its absence is not evidence that the player remains online. |
| [Frontier: Trailblazers Update 1, 6 March 2025](https://forums.frontier.co.uk/threads/elite-dangerous-trailblazers-update-1-thursday-6th-march.634606/) | Official notes list `CarrierLocation` among newly logged entries. They also state that `Location`/`FSDJump` Powerplay data is supplied regardless of whether the commander is pledged. These notes do not specify all `CarrierLocation` triggers or cancellation ordering. |
| [EDDI: Carrier location event](https://github.com/EDCD/EDDI/wiki/Carrier-location-event) | “Triggered at startup and shortly before a monitored fleet carrier arrives at a new destination.” Reports carrier ID/type, system name/address, and orbit BodyID. This is destination evidence and can precede completed arrival. |

The sources inspected do **not** guarantee that actions by another squadron
member, or actions while this player is offline, will be replayed into this
player's local journal on reconnect. Treat the journal as observations from
the local client, with possible gaps, rather than a complete carrier audit
log. A login snapshot can establish where the carrier is now without
establishing who moved it, how many jumps happened, or whether an old
scheduled jump was cancelled and replaced.

## Startup location versus live destination broadcast

`CarrierLocation` has two documented triggers. The parser uses journal
context to choose a label; wall-clock time or a pending request alone is
not a substitute for session evidence.

| Context | Notification |
| --- | --- |
| Missing session context, new part-one header (`Fileheader` or `FileHeader`), `Commander`, `LoadGame`, `Shutdown`, or `Resurrect` without subsequent gameplay | **Carrier has been located** (`location_changed`); never completes an earlier schedule. |
| `Location`, `FSDJump`, `Docked`, `Undocked`, `CarrierJumpRequest`, `CarrierJump`, `Embark`, or `Disembark` has established gameplay since the last boundary; player away | **Carrier Broadcast: New Arrival Destination** (`carrier_arrival`) for a changed destination or matching due schedule. No local request is required. |
| Player aboard a tracked pending jump | Wait for the richer `CarrierJump` and **Carrier Jump complete**; preserve this context across verified continuation parts. |
| Plugin starts with existing journal records | Reconstruct silently; neither login reports nor destination broadcasts are backfilled. |

`LoadGame` itself does not establish that a subsequent carrier report is
live: the supplied journal's startup `CarrierLocation` follows `LoadGame`
but precedes `Location`. A running plugin gives one located notification
per carrier on login even when the stored position is unchanged. Repeated
identical startup reports are suppressed; warm tailing restores that context
without replaying notifications. A later load boundary permits a new login
notification. Verified continuation files preserve the active-game context;
unverified gaps reset it.

The broadcast shares the completion embed's fields, green color, and
thumbnail, with the new title. System details come from the latest earlier
matching `FSDJump`, preferring `SystemAddress` and rejecting conflicting
addresses. These fields reflect that earlier visit, not a fresh server
query. The carrier event supplies the timestamp and any named orbit body;
the player's arrival star is never copied. A BodyID alone does not provide
a body name. Missing visits leave unavailable fields out of the embed.

A matching offboard destination report resolves the tracked pending
schedule and retains its request identity, but does not claim completed
arrival. Address/body checks, scheduled-departure checks, explicit
cancellation handling, and suppression of unchanged origin reports remain.
The broadcast supersedes an unsent schedule in the delivery queue; repeated
destination reports or a later unchanged `CarrierJump` do not announce the
movement twice. A different observed destination can broadcast without
crediting the mismatched request. This policy depends on the event sequence
available in the local journal; it cannot recover missing offline actions
or guarantee ordering for every game/client version.

## Evidence from the supplied journal

`Journal.2026-10-09T133657.01.log`, callsign `CCGL`, CarrierID `3715894016`:

| Line | UTC timestamp | Observation |
| --- | --- | --- |
| 12 | 10:38:52 | `CarrierLocation`: carrier is in Merope, before the player's startup `Location`. |
| 15 | 10:40:14 | `Location`: player is docked at CCGL in Merope. |
| 85 | 11:40:43 | `Undocked`: player leaves CCGL. |
| 231 | 11:49:28 | `FSDJump`: player visits Core Sys Sector FH-M a7-1, address `22957136956264`; arrival body is star A. |
| 321 | 11:52:49 | `CarrierJumpRequest`: carrier targets that system, body B 4, `DepartureTime` 12:08:10. |
| 432 | 12:08:10 | `CarrierLocation`: carrier reports the destination system, BodyID 6. |
| 437 | 12:09:02 | `Docked`: player docks at CCGL after its movement. |

There is no `CarrierJump` for this movement in the supplied file. The earlier
matching `FSDJump` provides Federation allegiance, Corporate government,
High Security, Djabushis Systems as controlling faction, and Powerplay
details. Copying those system fields is appropriate. Copying its star A as
the carrier's orbit body would be wrong; the request targets B 4.

The supplied file shows both a startup-position observation and a
post-request destination observation using `CarrierLocation`. It therefore
does not support treating every `CarrierLocation` as a fresh completed jump.
Its timing is observed evidence for this file, not a guaranteed one-minute
relationship between `CarrierLocation` and `CarrierJump` in every session.

## Original findings, ordered by priority

### R1 — P1: startup replay sends historical events as current notifications

In [listener.py](../listener.py), `main_loop()` sends every payload returned
by `refresh_from_journal()` to `announce()` (lines 240–245). Replay is not
restricted to recent/live records and does not discard a schedule that has
already been cancelled later in the same file.

Reproduction: start EDMC with an uncached journal containing
`CarrierJumpRequest -> CarrierJumpCancelled`. The announcer publishes both
`jump_started` and `jump_cancelled` even though the schedule is already over.

There is also a replay-state mismatch: if the persisted location is the
final destination, replaying an earlier origin snapshot generates a new
`location_changed` for the old origin. On the first run, that snapshot may
have emitted nothing because Origin was already known, so deduplication has
no corresponding payload to suppress after restart.

Recommendation: separate reconstruction from notification delivery. Seed
state silently, persist a journal cursor/event identity, and define an
explicit catch-up policy for records the announcer missed. A backfilled
message should be clearly identified as historical if backfill is enabled.

Reproductions: `test_startup_does_not_publish_already_cancelled_schedule`,
`test_restart_does_not_publish_old_origin_as_a_new_location`.

### R2 — P1: a startup or resurrection Location can falsely complete a jump

In [journal_parser.py](../journal_parser.py), the `Location` branch calls
`CarrierState.update_location()` for a watched docked carrier (lines
253–261). In [carrier_state.py](../carrier_state.py), `update_location()`
unconditionally emits `jump_completed` whenever `jumping` is true (lines
66–77), without checking that the observed system is the requested
destination or that this is an arrival event.

Reproduction: pending Origin -> Destination, then `Location` says the
player is docked at the carrier in Origin. The code announces completion
in Origin. A new `Location` in a third system also incorrectly consumes
the old pending jump as completed.

Recommendation: distinguish snapshots from actual transitions, and match
the scheduled destination by system address where available. A snapshot
alone should reconcile location rather than prove a particular jump.
Keep compatibility with older clients that used `Location` during carrier
travel only when the surrounding session/event context supports it.

Reproduction: `test_login_location_at_origin_does_not_complete_pending_jump`.

### R3 — P1: pending jumps survive offline gaps without reliable reconciliation

`JournalTailer.poll()` resets journal history when changing files but retains
the registry's `jumping` and `destination`. `LoadGame` and `Shutdown` do not
reconcile this pending state. Conversely, restarting the announcer restores
only `last_known_location`; pending state depends on whether its request
is still in the newest journal. Thus the result differs depending on whether
EDMC stayed running.

Reproduction A: request Origin -> Destination, exit the game, then log back
in before `DepartureTime`. A `CarrierLocation` at Origin triggers
`jump_unexpected_cancel`, even though the jump may still be scheduled.

Reproduction B: request an intra-system body move, go offline, then receive
an unchanged-system snapshot at login. The new offboard-completion path
announces `jump_completed`, even if somebody cancelled the move while the
player was offline. Destination-name equality cannot prove a body move.

If another person moves the carrier while the player is offline, the new
position may also match an old request by coincidence. That does not prove
the original schedule succeeded.

Recommendation: track session/load boundaries and mark old pending requests
as unresolved across observation gaps. Reconcile reconnect snapshots as
current location; do not infer success or cancellation solely from a system
name. Store request identity, destination address/body, and departure time
for correlation. Even a passed departure time is not proof of cancellation.

Reproductions: `test_relogin_before_departure_does_not_invent_cancellation`,
`test_same_system_relogin_snapshot_does_not_prove_offline_jump_succeeded`.

### R4 — P1: partial journal writes can discard a cancellation permanently

`JournalTailer.poll()` reads through EOF and advances `_offset` before
parsing the lines (journal_parser.py lines 361–366). If the last JSON line
is incomplete, `_parse_entry()` rejects it, and the next poll reads only
the remaining suffix. The complete event is never reconstructed.

Reproduction: append half a `CarrierJumpCancelled`, poll, append the rest
and an unchanged-system `CarrierLocation`. The cancellation is lost and
the pending intra-system jump is announced as completed.

Recommendation: retain an incomplete final line, or leave the committed
cursor before it until the terminating newline arrives. Apply the same
rule during startup replay. Drain unread records from the previous file
before switching to a newer part/session.

Reproduction: `test_cancel_split_across_writes_is_not_lost`.

### R5 — P2: continuation parts discard docking and system-visit context

The new `_JournalHistory` is reset for every filename change
(journal_parser.py lines 340–346). The documentation explicitly allows
another part of the same playing session; it need not contain another
player `Location`/`Docked` record before a carrier arrival.

Reproduction: `Docked -> CarrierJumpRequest -> Continued`, then part 2
starts with `FileHeader(part=2) -> CarrierLocation`. The code forgets that
the player is aboard, emits early completion, and suppresses the later
richer `CarrierJump`. It also loses earlier matching FSD system metadata.

Recommendation: preserve observation context across a verified continuation
of the same session, while resetting/reconciling it on a new load or
commander. Use `Continued`, header part numbers, and commander/session
identity rather than filename inequality alone.

Reproduction: `test_continuation_part_keeps_player_aboard_until_carrier_jump`.

### R6 — P2: 24-hour deduplication cannot suppress unlimited journal replay

[event_cache.py](../event_cache.py) removes announced payloads older than
24 hours on load (lines 126–129), but startup reads the entire newest
journal without the same age limit. Restarting after that retention period
can re-send old schedules, arrivals, or cancellations.

The key also depends on payload timestamp and event type. If completion
changes from `CarrierJump` to an earlier `CarrierLocation`, an older cached
completion with the later timestamp will not identify the new payload as
the same jump. This is an additional migration risk, not a replay guarantee.

Recommendation: persist consumed journal event identities/cursors
independently of log pruning; correlate alternative arrival observations
to a stable jump identity. Retention should not make consumed records new.

Reproduction: `test_old_announced_event_remains_deduplicated_after_cache_ttl`.

### R7 — P2: failed Discord delivery is recorded as already announced

In `announce()` (listener.py lines 195–208), an HTTP error or request
exception is logged, then the payload is still recorded in the dedup cache.
Later retries/restarts therefore suppress a message Discord did not accept.

Recommendation: separate pending delivery from accepted delivery. Persist
an outbox, handle Discord rate limits/retryable failures, and record
acknowledged sends. A timeout after server acceptance or a crash before
recording acknowledgement remains ambiguous; plain HTTP POST plus a local
cache does not provide an exactly-once delivery guarantee.

Reproduction: `test_failed_webhook_is_not_marked_successfully_announced`
uses a mocked HTTP 500; it sends no real webhook.

## Scenario checklist after fixes

| Scenario | Current behavior |
| --- | --- |
| Live request, then explicit cancellation | Announces cancellation; never completes the cancelled request. If its schedule is still unsent, that obsolete schedule is removed from the outbox. |
| Already-cancelled schedule present at EDMC startup | Reconstructs state silently; sends neither historical schedule nor cancellation. |
| Online player away; matching live destination report | Announces one **Carrier Broadcast: New Arrival Destination** after the scheduled departure, enriched from the latest earlier matching FSD visit. |
| Online player away; remote move with no local request | A changed destination produces the same broadcast, without attributing it to a request or operator. |
| Online player aboard | Waits for richer `CarrierJump`, including across verified continuation files. |
| Player logs off; no further journal records | Sends no inferred completion or cancellation. Clean shutdown invalidates pending schedules. |
| Player reconnects before departure | Reconnect/load boundaries invalidate uncertain schedules; startup report uses **Carrier has been located**, never cancellation or completion. |
| Somebody moves the carrier while player is offline | A startup report observed by a running announcer sends `location_changed`; it does not credit the old request or identify an operator. If EDMC also restarts, reconstruction is silent. |
| Offline intra-system movement or cancellation | Login snapshots never prove a body move. Live pending moves check the target BodyID when available. |
| Several offline jumps | Reconciles final observed position; intermediate jumps are not invented. |
| EDMC restarts on the same journal | Saves final reconstructed location silently; accepted identities and genuinely pending deliveries survive. |
| Same session continues in another part | Preserves docking/visit context after verifying continuation; drains unread old records first. |
| Cancellation or UTF-8 JSON arrives across writes | Keeps the incomplete byte sequence unconsumed until its terminating newline arrives. |
| HTTP 500, timeout, or rate limit | Retains the delivery and retries with backoff; only confirmed HTTP acceptance records success. |
| Webhook edited or carrier removed | Pending delivery follows the new configured route; removing the route removes its queued deliveries. |
| Commander changes or an old journal is touched | FID/session boundaries invalidate pending state; a changed FID also clears visit metadata. Active-journal selection uses chronological journal names rather than modification time. |

## Verification and remaining limits

Run:

```bash
python3 -m unittest discover -s tests -v
```

**77 tests pass with no expected failures.** The original nine expected-failure
markers have been removed. Coverage includes the review reproductions,
startup-to-live polling, preceding continuation parts, departure/body/address
matching, split cancellation and UTF-8 records, retry persistence, rate limits,
queue supersession, route updates, write failure before POST, and accepted
send recovery if outbox cleanup fails. Additional coverage checks startup
versus active-game labels, remote arrivals without requests, login duplicates
and load resets, real `Fileheader` spelling, warm-context restoration,
continuation broadcasts, visual parity with completion, and persistent queue
supersession by broadcasts. HTTP is mocked; journals and delivery
state use temporary files or patched collaborators.

The full attached CCGL journal was additionally replayed to verify silent
startup and final location. Splitting it before the arrival and reading the
rest live yields exactly one destination broadcast at 12:08:10 with the matching FSD
system details. The player's arrival star is not copied as the carrier's
orbit body.

`DepartureTime` is displayed as **Scheduled Departure**. The previous fixed
request-time-plus-16-minutes arrival guess has been removed.

A timeout after Discord accepts a POST, or a crash before its acknowledgement
is recorded, remains ambiguous: retries can duplicate an accepted message.
The outbox provides persistent retry and acknowledgement tracking, not an
exactly-once guarantee. Permanent HTTP failures remain pending with bounded
backoff; correcting/removing the configured webhook updates/stops retries.
A local storage failure can stop the worker; queued deliveries persist, and
HTTP is not attempted if the outbox cannot be written first. Accepted identities
are retained in the event log, so its notification history can grow over time.

Actions while the player is offline are limited to what the local journal
actually reports after reconnect. Unknown intermediate jumps, cancellation
history, and operators are not reconstructed. Startup intentionally discards
historical notification payloads, so events not queued while the announcer was
running are not backfilled. Real journals would still help validate all
squadron/interior/client-version combinations beyond the documented and
observed sequences; missing journal observations cannot prove an outcome.

## Russian journal investigation, 2026-10-10

The second supplied attachment, `message.txt`, contains 3,414 valid UTF-8
JSON journal records with `language: Russian/RU`. Event names, carrier IDs,
station types, and system identities retain the same representation used by
the parser. Russian `SystemGovernment_Localised` and
`SystemSecurity_Localised` values are display fields, not matching criteria.

The reported symptom is **no notifications at all**. The journal alone does
not reproduce that symptom when the moving carrier's numeric identity is
configured. Reading its records as live observations detects four schedules,
two aboard completions, and two offboard destination broadcasts. Both
completion embeds render their Russian display values successfully. A full
startup replay intentionally produces no notifications, as with English
journals. This validates parsing and embed construction; it does not verify
the friend's installation, settings, or actual Discord delivery.

| Carrier in this attachment | Evidence |
| --- | --- |
| `XFH-27F`, CarrierID `3715921920` | All four jump requests/movements belong to this fleet carrier. Aboard completions are HR 5518 at 07:32:13 UTC and HR 5307 at 08:32:11 UTC. Offboard destination reports are Praea Euq PY-Y b1-0 at 07:56:10 UTC and Skeggiko O at 09:26:10 UTC. |
| `CCGL`, CarrierID `3715894016` | Squadron-carrier startup reports show Eta Cassiopeiae. This attachment contains no jumps for that carrier. |

Two configuration-dependent results were reproduced:

- Watching `XFH-27F` **without** its numeric CarrierID misses all four
  `CarrierJumpRequest` and both offboard `CarrierLocation` broadcasts:
  these events supply the ID but no callsign. The two aboard `CarrierJump`
  records still produce generic located messages through `StationName`.
  This is a partial-notification failure and is not sufficient evidence to
  explain the friend's reported complete silence.
- Watching only `CCGL` correctly produces no jump notifications for the
  other carrier. Its startup reports can produce located messages only if
  observed after the plugin's silent startup replay.

No arrival report in this attachment has a preceding matching `FSDJump`
for its destination. The two offboard broadcasts therefore lack government,
security, faction, and Powerplay enrichment. That data absence does not
prevent sending the broadcast. The aboard `CarrierJump` records supply
their own system details.

The initial journal-only investigation could not establish the installation's
failure. The subsequently supplied EDMC logs and confirmed settings resolve
that uncertainty, as described below. The localized fields themselves do
not require a parser change.

`tests/fixtures/russian_carrier_session.jsonl` keeps 27 relevant observations
from the attachment and excludes unrelated personal/chat/inventory fields.
Six tests in `tests/test_localized_journal.py` exercise native Unicode with
Windows CRLF, silent startup followed by live polling, all four movements,
Russian embed fields, absent numeric identity, separation of carrier IDs,
missing prior destination visits, and equivalent movement detection when
display strings are replaced with English. No real webhook is called.

### Confirmed cause: legacy filenames sort after modern filenames

The friend confirmed `XFH-27F`, CarrierID `3715921920`, name Parallax. The
debug log confirms those settings were loaded correctly. The new build's
worker starts normally, but repeatedly reports:

```text
04:38:27  Reading Journal.261001214723.01.log
07:20:50  Reading Journal.261001214723.01.log
07:38:33  Reading Journal.261001214723.01.log
07:49:47  Reading Journal.261001214723.01.log
07:50:06  Reading Journal.261001214723.01.log
```

That legacy filename encodes **2026-10-01 21:47:23**, whereas an older plugin
build run at 07:50:48 reads `Journal.2026-10-10T073649.01.log`, reconstructs
the expected carrier positions, and processes jump payloads. The startup
messages and source line numbers distinguish the two implementations even
though development builds share the same displayed version.

The regression came from selecting journals by raw lexical filename order
instead of modification time. `Journal.26...` sorts after `Journal.2026-...`
even when it represents an earlier date. Both newest-file selection and
the tailer's filename-range comparisons shared this bug. The new worker
therefore keeps watching the old file while the game writes the modern one,
explaining the missing live notifications. The EDSM connection-reset
tracebacks belong to another plugin and do not explain this EDFCA behavior.

**Fixed:** parse both `YYMMDDHHMMSS` and `YYYY-MM-DDTHHMMSS` filename dates,
then order by actual session date and numeric part. Startup selection,
preceding-part discovery, live switching, and draining intervening files use
that ordering. Invalid journal names are skipped. Modification time still
does not make a copied/touched old journal current. Existing cancellation,
offline-boundary, and continuation rules remain in effect.

Seven tests in `tests/test_journal_ordering.py` cover the exact mixed-name
reproduction, live rotation from legacy to modern names, chronological
intervening sessions, newer legacy files, legacy continuation, malformed
names, and omission of webhook credentials from state debug output. The
original startup/rotation/order reproductions fail before the fix and pass
after it.

The supplied debug log also contains webhook credentials in carrier-state
and older-build payload dumps. They are not copied into this project note
or fixtures. `CarrierState` now excludes its webhook from its dataclass
representation, so the current worker's state-debug log retains useful
position data without including the credential. Existing exposed webhook
credentials should be regenerated and the configured routes updated.
