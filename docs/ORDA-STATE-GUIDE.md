# Orda State — Operator Guide (Public)

**Version:** v1 contract (schema_version 1, locked 2026-09-18)
**Audience:** operators running Orda sessions on a single macOS or Linux machine
**Status of this document:** every command and exit code in this guide was
verified against the live CLI and the test suite on 2026-09-18. Statements
about unimplemented or planned behavior are marked explicitly.

---

## 0. Quick start

```sh
# 0. Point the examples at your install (portable placeholders used throughout)
export HERMES_HOME="$HOME/.hermes"            # adjust if your install lives elsewhere
export ORDA_HOME="$HERMES_HOME/eldunari/nexus/state/orda"
export ORDA="python3 $ORDA_HOME/tool/orda_state.py"
# If your distribution ships a wrapper, this is equivalent:
#   export ORDA="orda-state"

# 1. One-time: create the state home
$ORDA init

# 2. Every session: pick a unique session id, read the brief, claim the seat
export ORDA_SESSION="my-session-2026-09-18-a"
$ORDA brief                     # one small read — the current state of the world
$ORDA claim --session "$ORDA_SESSION" --ttl 900

# 3. Do work through the CLI, never by hand-editing state files
$ORDA set-project --session "$ORDA_SESSION" --slug my-project \
    --status running --summary "What I am doing" --next "The next concrete step"
$ORDA event --session "$ORDA_SESSION" --kind decision \
    --project my-project --data '{"decision":"why this choice"}'

# 4. During long work: renew the lease before it expires
$ORDA renew --session "$ORDA_SESSION"

# 5. When done: release the seat
$ORDA release --session "$ORDA_SESSION"

# 6. Health check any time (read-only, no seat needed)
$ORDA verify
```

If you only remember three rules:

1. **Reads are free; writes need the seat.** Claim before you write.
2. **events.jsonl is the truth; state.json is a cache.** Never hand-edit either.
3. **Never put secrets in state, events, projections, or handoff bundles.**

---

## 1. What Orda state solves — and what it does not

Orda is a continuity system for AI-agent (or human) sessions that share one
operational workload across time. It answers: *when a new session starts, how
does it know exactly where things stand without reading a transcript?*

It solves:

- **Crash-proof continuity.** A session that dies mid-work leaves an auditable
  event log; the next session rebuilds state from it in seconds.
- **One-writer safety.** Multiple concurrent sessions can read freely, but
  exactly one holds a lease to write. No silent clobbering, ever.
- **Provenance.** Every change is an append-only, hash-chained event: who wrote
  it, when, and on top of what. Tampering is detectable.
- **Compact handoff.** A generated projection (the "brief") carries statuses,
  decisions, blockers, next actions, and pointers — never full transcripts.

It does **not** solve:

- **Multi-machine coordination.** The write lock (POSIX `flock`) is local to one
  filesystem. Two machines pointing at different homes are two unrelated
  systems; two machines sharing one network mount is unsupported.
- **Authentication/authorization.** Anyone with filesystem read access to the
  home can read; anyone with write access can claim the seat. Security is
  filesystem permissions, not passwords.
- **Rich querying.** The projection is compact by design; deep history means
  reading events.jsonl.
- **Secrets storage.** There is no encrypted secret store here, by design.
- **Real-time messaging.** The brief is read at session start; there is no
  notification channel between sessions.

---

## 2. Canonical state location and file layout

Default home (overridable for every command with `--home PATH`):

    $HERMES_HOME/eldunari/nexus/state/orda/

Layout:

    <home>/
      state.json        Materialized current state: revision, epoch, projects{}
                        (a rebuildable cache — see §10)
      events.jsonl      Append-only hash-chained event log. THE TRUTH. (§10)
      seat.json         Current seat lease: holder session, expiry, epoch (§5)
      .lock             The flock target for every mutation (0600)
      events/           Reserved directory (currently empty)
      projection/
        brief.md        Compact human brief (generated; do not hand-edit)
        brief.json      Same brief, machine-readable
      tool/
        orda_state.py   CLI entry point
        orda_store.py   Store: leases, CAS, hash chain, atomic writes
        orda_brief.py   Projection builder and markdown renderer
        orda_taskhome.py  Legacy TASK-HOME.md parser (migration surface)
        tests/          Test suite (unit + real multi-process concurrency)
      ARCHITECTURE.md   Design record: invariants I1–I8, decision history
      MIGRATION.md      Migration/handoff receipt from the cutover to v1

Installed file permissions: `state.json`, `events.jsonl`, `seat.json`, `.lock`
are `0600` (owner-only); documentation is `0644`. Keep it that way (§14).

**Rule of ownership:** everything under the state home is either generated
(`state.json`, `events.jsonl`, `projection/`) or documentation. The only things
a human may add are handoff bundles written to a location *outside* the home.

---

## 3. First-time setup and prerequisites

Prerequisites:

- Python 3.7+ (verified on 3.9). **Standard library only** — no packages,
  no pip, no network access.
- macOS or Linux (the write lock uses POSIX `flock`; Windows is not supported).
- Write access to the state home directory.

First-time setup:

```sh
$ORDA init          # creates the home; prints {"initialized": ..., "revision": 0}
```

`init` is safe to detect mistakes: running it on an already-initialized home
exits 2 with "already initialized" and changes nothing.

What init creates: `state.json` (revision 0, epoch 1, empty projects),
an empty `events.jsonl`, `seat.json` with no holder, `projection/`, `events/`,
and `.lock`.

Recommended: put the state home under version control (the reference install
keeps it inside a git repository). This gives you audit history, off-machine
backup, and the rollback path in §16 for free.

---

## 4. Starting a session

Every session (agent or human operator) follows the same three steps:

```sh
# a) Read the brief — one small read, never an old transcript
$ORDA brief                 # markdown; add --format json for machines

# b) Choose a UNIQUE session id (unique across all sessions that ever ran)
export ORDA_SESSION="feature-x-2026-09-18-1"

# c) Claim the writer seat
$ORDA claim --session "$ORDA_SESSION" --ttl 900
```

A successful claim prints the lease: your session id, `acquired_utc`,
`expires_utc`, `epoch`, and any takeover note. `--note` lets you label the
claim for the audit trail (e.g. `--note "continuing lane feature-x"`).

Check the seat at any time (read-only):

```sh
$ORDA seat
```

`seat` shows the current holder, expiry, epoch, and a boolean
`valid_for_writes` (true only while the lease is unexpired).

Session id rules:

- Must be unique enough to identify the session in the audit log. A slug plus
  a date plus a counter works well.
- It is your identity for `claim`/`renew`/`release` and every write. Losing it
  means you cannot renew or release your own lease (§6 recovery covers this).

---

## 5. Writer-seat semantics

The model is **one writer, unlimited local readers**:

- **Reads never require a lease.** `brief`, `seat`, `verify`, `export` work
  from any session, any number at a time.
- **Writes require a live lease** matching your session id: `set-project`,
  `event`, `reconcile`, `import`, `migrate`. Without it you get exit 4.
- **Assignment is not automatic.** Nobody hands you the seat; you claim it
  explicitly with `claim`. If another live session holds it, your claim is
  refused with exit 4 and the holder's id and expiry in the message.

Claim outcomes (all verified live):

| situation | result |
|---|---|
| seat free | you hold it; epoch unchanged |
| seat held by *you*, still live | refresh ("renew-on-claim"); epoch unchanged |
| seat held by someone else, still live | refused, exit 4, unless `--steal` |
| seat expired (crashed/abandoned predecessor) | **stale-lease recovery**: your claim succeeds, epoch bumps +1, a `takeover` event records the previous holder |
| `--steal` over a live holder | **recorded forced takeover**: a `takeover` event with mode `stolen-from-live-holder` names the displaced session |

`--steal` is an explicit, audited decision — the event log permanently records
who took the seat from whom. It exists so a stuck session never deadlocks the
system; use it only after confirming the holder is genuinely dead or wrong
(check `seat` and the holder's expected lifetime first).

Renew and release:

```sh
$ORDA renew --session "$ORDA_SESSION" --ttl 900   # extend from now
$ORDA release --session "$ORDA_SESSION"           # free the seat immediately
```

- `renew` fails with exit 4 if the lease already expired — reclaim instead.
- `renew` fails with exit 4 if your session id doesn't match the holder.
- `release` by a non-holder is also refused (exit 4); releasing an
  already-free seat is a harmless no-op that just records the release time.

Multiple concurrent Orda sessions in practice:

- Sessions A and B both start, both read `brief` — fine, unlimited.
- A claims. B claims → exit 4. B works read-only, or waits, or (with an
  explicit decision) takes over with `--steal`.
- A's lease expires un-renewed. B claims → succeeds as stale-lease recovery;
  epoch bumps; the log shows A lost the seat at that moment.
- Several worker *processes* of the SAME session id can write concurrently:
  the flock serializes them and the CAS on revision keeps the event log
  consistent (this exact scenario is covered by a real multi-process test).

---

## 6. TTL behavior, renewal, and crashed sessions

- Default TTL is **900 seconds (15 minutes)**; set your own with
  `claim --ttl N` / `renew --ttl N`. `seat.json` stores `ttl_s` and
  `expires_utc`.
- The TTL is a **liveness assertion, not a timer that interrupts you.** Nothing
  kills your work at expiry — but at expiry your *next write* fails with exit 4
  and another session may claim stale-lease recovery.
- Renewal expectation: renew at least once per TTL while you hold the seat
  during long work. A practical pattern is to renew at every natural checkpoint
  (after each batch of writes). Renewal extends from *now*, not from expiry, so
  renewing early never loses time.
- If a session **crashes or forgets to release:**
  - the lease simply expires after its TTL; nothing is corrupted;
  - the next `claim` performs stale-lease recovery automatically (epoch bump,
    `takeover` event) — no operator intervention needed;
  - the dead session's last writes are fully durable (§10 crash windows);
  - the dead session cannot come back and write, because its id no longer
    matches the holder — it gets exit 4 and must re-claim.
- Forcing over a *live* seat always requires `--steal` and is recorded.

---

## 7. Command reference

Conventions: `$ORDA` is the CLI (§0), `$ORDA_SESSION` your session id.
All commands accept `--home PATH` to override the state home.
Exit codes: **0** ok · **2** usage · **3** revision conflict · **4**
lease/seat refusal · **5** integrity failure. (An unexpected Python traceback
exits 1 — treat it as a bug, not an expected code.)

### init — create the state home (one time)

```sh
$ORDA init
# → {"initialized": ".../orda", "revision": 0}
```

### seat — inspect the current lease (read-only)

```sh
$ORDA seat
# → holder, session, acquired/renew/expires UTC, ttl_s, epoch, note,
#   valid_for_writes
```

### claim — acquire the writer seat

```sh
$ORDA claim --session "$ORDA_SESSION" [--ttl 900] [--steal] [--note "..."]
```

See §5 for the outcome table.

### renew — extend your lease

```sh
$ORDA renew --session "$ORDA_SESSION" [--ttl 900]
```

### release — free the seat

```sh
$ORDA release --session "$ORDA_SESSION"
```

### set-project — upsert a project record

```sh
$ORDA set-project --session "$ORDA_SESSION" --slug my-project \
    [--status running|awaiting-owner|blocked|paused|followup|closed] \
    [--summary "one-line what/why"] \
    [--next "the next concrete action"] \
    [--blocker "first blocker;second blocker"] \
    [--detail-ref "docs/notes.md#my-project"] \
    [--expected-rev 41]
```

- Fields are merged onto the existing record; omitted fields are left alone.
- `--blocker` is a semicolon-separated list and replaces the whole list.
- `--expected-rev N` enables optimistic concurrency: the write lands only if
  the current revision is still N; otherwise exit 3 (§10).
- Status values are free text, but the projection groups records under the six
  sections above — using those values keeps the brief tidy.
- **Accuracy note:** `ARCHITECTURE.md` lists a `--tag` flag in its command
  table, but the v1 CLI does not implement it (verified against the source).
  Tags enter records only through `migrate` from a legacy TASK-HOME file, or a
  future CLI version. Do not script against `--tag`.

### event — append a decision / conflict / note / custom event

```sh
$ORDA event --session "$ORDA_SESSION" --kind decision \
    [--project my-project] [--data '{"key":"value"}'] [--note "..."] \
    [--expected-rev 41]
```

- `--data` must be valid JSON (exit 2 otherwise). The suggested kinds are
  `decision`, `conflict`, `note`; other custom kinds are accepted.
- Decisions and the last 10 conflicts/takeovers surface in the projection.
- Event data is payload-light by convention: record the *decision and its
  pointer*, not the whole document (§13).

### brief — read the compact projection (read-only)

```sh
$ORDA brief                    # markdown to stdout
$ORDA brief --format json      # machine-readable
$ORDA brief --write            # also (re)write projection/brief.{md,json}
```

Output: a header line (revision, epoch, seat holder + expiry, last writer),
status sections (`RUNNING`, `AWAITING OWNER`, `BLOCKED`, `PAUSED`,
`OPEN FOLLOW-UPS`, `CLOSED`), unresolved conflicts, recent decisions, and
pointers to the underlying files. Summaries are capped (~220 chars) — this is
a cockpit read, not an archive.

### verify — integrity check (read-only)

```sh
$ORDA verify
# → {"ok": true, "problems": [], "events": 117, "revision": 117, ...}
```

Checks the hash chain, revision consistency between state and event log, and
projection freshness. Any problem ⇒ report on stdout, `ERROR[5]` on stderr,
exit 5.

### reconcile — rebuild state.json from events.jsonl

```sh
$ORDA reconcile --session "$ORDA_SESSION"    # requires the lease
```

Repairs a crash window (state behind the log). **Does not repair a tampered
event log** — see §10 and §16.

### migrate — import a legacy TASK-HOME-style file (idempotent)

```sh
$ORDA migrate --session "$ORDA_SESSION" --source TASK-HOME.md [--label "..."]
```

Parses `## SECTION` headings (RUNNING / AWAITING OWNER / PAUSED /
OPEN FOLLOW-UPS / CLOSED) and `- slug: TAG — summary` bullets into project
records. Idempotent: unchanged records re-run clean (`changed: 0`). Every
parsed slug is guaranteed to land (zero-loss). The source file is **never
modified or deleted**; each record's `detail_ref` points back into it.

### export — bounded handoff bundle

```sh
$ORDA export --out /tmp/handoff.json
# → {"kind": "orda-handoff", "schema_version": 1, "exported_utc": ...,
#     "from_session": ..., "brief": {...}}
```

Projection + references only. **Never contains a transcript.** Write the
bundle outside the state home.

### import — record a received handoff bundle

```sh
$ORDA import --session "$ORDA_SESSION" --file /tmp/handoff.json
```

Validates the bundle kind and appends a `handoff-import` event (additive;
nothing overwrites). Requires the lease.

---

## 8. Exit codes and recovery actions

| code | meaning | typical trigger | recovery |
|---|---|---|---|
| 0 | success | — | — |
| 2 | usage error | unknown command; missing/invalid flag; `--data` not JSON; `init` on initialized home; `import` of a non-orda-handoff file; source file missing | fix the command line; read the message |
| 3 | revision conflict (CAS) | `--expected-rev` no longer matches current revision | re-read state (`brief` or `set-project` without `--expected-rev` to see current values), re-apply your change on top of the winner's version, retry with the new revision. Never "force" — the loser's data was rejected, not half-applied |
| 4 | lease/seat refusal | write without claiming; expired lease; `claim` over a live foreign seat; `renew`/`release` by a non-holder or after expiry | `seat` to see who holds it. Yours expired → `claim` again. Someone else live → coordinate or `--steal` as an explicit decision. Someone else's stale seat → plain `claim` (auto stale-recovery) |
| 5 | integrity failure | hash-chain mismatch or tampered event; state/event revision mismatch; stale projection | See §10/§16: crash-window → `reconcile`; tampered log → restore from git/backup; stale projection → `brief --write` after repairing |

---

## 9. Architecture at a glance

```
                read (no lease needed)
   any session ──────────────────────────────────────────────┐
        │                                                    │
        │ write (needs live lease)                           ▼
        ▼                                              projection/brief.{md,json}
   ┌─────────┐  flock(<home>/.lock)   ┌────────────────┐        ▲
   │ seat    │───────────────────────▶│ event append    │        │ generated
   │ lease   │  CAS: expected-rev ==  │ (fsync, hash    │        │ after every
   └─────────┘  current revision?    │  chained)       │        │ mutation
        │             │ no→exit 3    └───────┬────────┘        │
        │             │ yes                  │                 │
        │             ▼                      ▼                 │
        │      ┌─────────────┐  atomic rename  ┌───────────┐  │
        └─────▶│ state.json  │◀────────────────│ events.   │──┘
               │ (cache)     │   rebuildable   │ jsonl     │
               └─────────────┘                 │ (TRUTH)   │
                                               └───────────┘
```

- **events.jsonl is the truth** (I1): every mutation is one appended event;
  `state.json` is a rebuildable cache. `reconcile` rebuilds it; nothing else.
- **Hash chain** (I2): each event carries `seq`, `revision`, UTC timestamp,
  writer session, kind, project, JSON data, `prev_hash`, `hash`
  (sha256 over the previous hash plus the canonical event JSON). Genesis
  `prev_hash` is 64 zeros. Any edit to history breaks the chain detectably.
- **Every mutation**: take exclusive flock on `<home>/.lock` → check the
  caller's lease → CAS the expected revision → append the event (fsync) →
  atomically rename the new `state.json` (I3, I4).
- **Crash windows**: a crash after the event lands but before the state rename
  leaves `state.json` exactly one event behind — never torn or partial.
  `verify` flags it ("state revision N behind event log revision N+1 — crash
  window; run reconcile"); `reconcile` repairs it. (Verified live.)
- **What reconcile cannot fix**: a tampered `events.jsonl`. The log is the
  truth, so there is nothing to rebuild *from*. Restore it from version
  control or backup instead (§16). Verified live: after tampering seq 1,
  `reconcile` rebuilt state but `verify` still exited 5 — by design.

---

## 10. Concurrency internals (CAS, flock, atomic writes)

- **CAS / revisions.** Every event bumps a global monotonic `revision`.
  A writer may pin `--expected-rev N`; if another writer moved the revision
  first, the write is rejected with exit 3 and the current revision and last
  writer are named. Without the flag, writes still serialize under the lock —
  the flag is for read-modify-write protection, not for correctness of the log
  itself.
- **flock.** All mutations take an exclusive `flock` on `<home>/.lock` for the
  duration of read-check-append-rename. Concurrent processes of the *same*
  session therefore lose nothing (each write lands, revisions stay monotonic);
  this is exercised by a real 4-writer × 10-write multi-process test.
- **Atomic writes.** `state.json`, `seat.json`, and projections are written to
  a temp file in the same directory, fsynced, then `rename(2)`d over the
  target — readers never see a partial file.
- **Takeover discipline.** Distinct sessions never overwrite each other
  silently: a second session must record an explicit `--steal` takeover, and
  each such takeover appears as a `takeover` event naming the previous holder
  (tested with 4 racing sessions: exactly the expected takeovers recorded).

---

## 11. Session handoff between models/sessions (no transcripts)

The protocol, end to end:

```sh
# ── Outgoing session ──────────────────────────────────────────────
$ORDA set-project ...        # leave statuses/summaries/next-actions current
$ORDA event --kind decision --data '{"decision":"...","why":"..."}'
$ORDA export --out /tmp/handoff.json   # optional point-in-time bundle
$ORDA release --session "$ORDA_SESSION"

# ── Incoming session (fresh context, same machine or same home) ───
$ORDA brief                            # ONE small read — no transcript
export ORDA_SESSION="next-session-id"
$ORDA claim --session "$ORDA_SESSION"
$ORDA import --session "$ORDA_SESSION" --file /tmp/handoff.json   # if exported
```

Why this works without transcripts: the projection already carries current
statuses, decisions, blockers, next actions, and unresolved conflicts; deeper
detail is consulted lazily from `events.jsonl` (searchable by seq/kind/writer)
and from `detail_ref` pointers into your own documents. The handoff bundle is
a convenience snapshot of exactly that projection — sharing it is optional.

---

## 12. TASK-HOME compatibility window and migration

If you are coming from a hand-edited markdown task home:

- `migrate --source TASK-HOME.md` imports it into the store — idempotently and
  with zero loss (every parsed slug is asserted present afterwards; the live
  cutover migration moved 102 records / 101 upserts and verified clean).
- The source file is never modified or deleted by the tool; it stays the
  pre-cutover record and remains readable.
- **During the compatibility window** (until all workflows write through
  `set-project`/`event` instead of editing the markdown): after any hand edit
  of the source file, run `migrate` again to re-sync. Unchanged runs are
  no-ops (`changed: 0`).
- Re-syncs are recorded as `migration` events with the source's content hash,
  so the audit log shows exactly which version of the file each sync saw.
- A tool-supplied `detail_ref` points into the source file (`path#slug`), so
  records imported this way still link to their original context.

---

## 13. What belongs where

| data | home |
|---|---|
| project statuses, summaries, next actions, blockers | `set-project` → canonical state |
| decisions, conflicts, notes (with pointers) | `event` → canonical state |
| pointers into detail documents | `--detail-ref` / `--data` (paths + anchors, not copies) |
| credentials, tokens, `.env`, auth files | profile-local secret storage — **never** the state home |
| process logs, transcripts, per-lane scratch, caches | profile-local cache/scratch directories |
| handoff bundles | any scratch/export location outside the state home |

Test to apply before any write: *"Would I be comfortable if this string appeared
in a file that is synced, backed up, and audited forever?"* If not, it is a
pointer, not payload (§14).

---

## 14. Security and privacy rules

1. **No secrets in state. Ever.** No tokens, passwords, private keys, account
   identifiers, personal data, or credential-adjacent material in
   `set-project` fields, event `--data`, the projection, or handoff bundles.
   The event log is append-only and hash-chained: **you cannot cleanly delete
   something once written** — correcting requires a compensating event, and the
   original remains in history.
2. **Local-machine scope.** The state home is single-machine by design. Do not
   sync it to shared network locations, and treat exports like any other
   operational artifact (scan before sharing).
3. **Filesystem permissions.** State files are created `0600`. Keep the home
   out of world-readable directories; multi-user machines should rely on
   per-user homes (`$HERMES_HOME` is user-scoped).
4. **Backups inherit the secrecy.** A git remote or backup of the state home
   carries everything in it — apply the same access discipline there.
5. **Handoff bundles are point-in-time state.** `export` output contains your
   project records, decisions, and references. Scan it for sensitive strings
   before sending it anywhere.

---

## 15. Known limits and breaking points (v1)

- **Single machine.** POSIX `flock` only; no distributed or multi-machine
  coordination. Two homes = two systems.
- **Single writer.** Deliberate: the seat models one operator. Throughput is
  not the goal; correctness of continuity is.
- **Unbounded event log.** `events.jsonl` grows forever. **Chain-preserving
  compaction (fold + archive with a head-hash anchor) is planned future work,
  not implemented in v1.** Until then: log size is the archive; disk is cheap;
  `brief` keeps reads compact regardless.
- **Compatibility-window discipline.** Until workflows stop hand-editing the
  legacy task home, forgetting `migrate` after a hand edit leaves the store
  stale until the next sync. Detectable (revision counters), but manual.
- **`--tag` not implemented in the CLI** (documented in ARCHITECTURE.md's
  command table, absent from the v1 code — verified). Tags arrive via
  `migrate` only.
- **No schema enforcement on event kinds/data.** Custom kinds and arbitrary
  JSON are accepted; discipline is conventional, not enforced.
- **No built-in auth.** Filesystem permissions are the whole security model.
- **Unexpected crashes exit 1** (Python traceback) rather than a documented
  code — treat as a bug report with the traceback.
- **Operational risks:** (a) a session id forgotten before release forces
  stale-recovery on the next claim (harmless but noisy); (b) a very long TTL
  (`--ttl 999999`) makes stale-recovery slow — keep TTLs realistic; (c) hand
  edits to `state.json`/`events.jsonl`/`projection/*` are tampering by
  definition — `verify` will flag them, and the fix is a restore (§16), not a
  patch.

---

## 16. Backup, verification, rollback, disaster recovery

- **Verification cadence.** Run `verify` at session start, after any anomaly,
  and before backups. Exit 0 + `"ok": true` is the health certificate.
- **Backups.** The state home is plain JSON/JSONL — commit it to version
  control or copy it like any file. Commit after meaningful milestones so the
  backup history aligns with revision history. (The reference install keeps
  the home inside a git repository.)
- **Rollback of state content.** Because the home is version-controlled, you
  can roll state back by restoring the files from an earlier commit — then run
  `verify` (expect exit 5 if you rolled back only `state.json`: the log will be
  "ahead" — restore `events.jsonl` from the same commit to keep them paired).
- **Crash-window recovery** (state one event behind log — flagged by verify):
  ```sh
  $ORDA claim --session "$ORDA_SESSION"
  $ORDA reconcile --session "$ORDA_SESSION"
  $ORDA verify          # expect exit 0
  ```
- **Tampered/broken event log** (hash mismatch — exit 5 persists after
  reconcile; verified live):
  ```sh
  # events.jsonl is the truth — repair means restore, not rehash:
  git -C <repo-with-the-home> checkout <last-good-commit> -- nexus/state/orda/
  $ORDA verify          # expect exit 0
  # No git? Restore events.jsonl (and state.json with it) from your backup.
  ```
  Never "fix" hashes in place: the chain is the tamper evidence, and rewriting
  history would make every future audit untrustworthy.
- **Stale projection** (verify reports "projection brief.json at revision X,
  state at Y"): any mutation regenerates it; or run `brief --write` once
  repaired.
- **Full disaster.** Worst case, the truth (`events.jsonl`) plus a seat/epoch
  you no longer care about is everything. Re-initializing (`init` fails on an
  existing home — move the old home aside first) and re-running `migrate` from
  your source documents reconstructs the operational surface; history is lost
  unless you kept the old `events.jsonl` — keep it.

---

## 17. Troubleshooting matrix

| symptom | likely cause | action |
|---|---|---|
| `ERROR[4]: no live lease for session 'x'` | never claimed, or lease expired | `$ORDA seat`, then `claim --session x` |
| `ERROR[4]: seat held by 'y' until T` | another live writer | coordinate; or confirm y is dead and `claim --steal` (recorded) |
| `ERROR[3]: revision conflict: expected N, current M` | concurrent writer since your read | re-read, merge your change on top, retry with the new revision |
| `ERROR[5]: seq N: prev_hash mismatch / hash mismatch` | event log edited or corrupted | restore `events.jsonl` (+`state.json`) from git/backup (§16) |
| `verify`: "state revision behind event log … run reconcile" | crash window | claim + `reconcile` + `verify` |
| `verify`: "projection … stale projection; run: brief --write" | projection older than state | `brief --write` after any repair |
| `ERROR[2]: already initialized` | `init` on existing home | nothing to do; use `seat`/`brief` |
| writes vanish / brief looks old | someone hand-edited the legacy task home without re-syncing | run `migrate --source ...` again |
| `ERROR[2]: --data must be valid JSON` | unquoted or malformed JSON | quote the payload; validate with `python3 -m json.tool` |
| `renew` says "lease already expired" | TTL lapsed | `claim` again (stale-recovery if free) |
| exit 1 + traceback | unexpected bug | capture output, report with traceback; state is consistent (mutations are atomic) |

---

## 18. Version and status labeling

- **v1 contract (implemented, tested, verified live 2026-09-18):** everything
  in §2–§17 — seat/lease semantics, CAS, hash chain, crash windows, projection,
  migrate/export/import, exit codes 0/2/3/4/5. Schema version: 1. Design
  invariants: I1–I8 in `ARCHITECTURE.md`.
- **Planned future work (NOT implemented in v1 — do not rely on it):**
  chain-preserving event-log compaction (fold + archive anchored at a head
  hash); moving dispatch workflows fully onto the CLI (ending the TASK-HOME
  compatibility window); possible `--tag` support in `set-project`.
- **Explicitly out of scope for v1:** multi-machine coordination, DB backends
  (a DB would have to justify itself against git + a read-only projection),
  secrets storage, notification channels.

Change discipline: v1 is locked; behavioral changes require a new design
decision record and a schema-version bump where representation changes.

---

## Appendix A. Verified-example transcript (2026-09-18, sandbox home)

```sh
$ orda-state init --session demo-init
{"initialized": ".../demo", "revision": 0}

$ orda-state claim --session demo --ttl 300 --note "guide verification session"
{ "acquired_utc": "2026-09-18T14:41:18Z", "epoch": 1,
  "expires_utc": "2026-09-18T14:46:18Z", "holder": "demo",
  "note": "guide verification session", "renew_utc": "2026-09-18T14:41:18Z",
  "session": "demo", "ttl_s": 300 }

$ orda-state set-project --session demo --slug example-project \
    --status running --summary "Working on the example feature" \
    --next "Run integration tests" \
    --blocker "needs API token;waiting on review" \
    --detail-ref "docs/example.md#example-project"
{"event_seq": 1, "revision": 1, "slug": "example-project"}

$ orda-state event --session demo --kind decision \
    --project example-project --data '{"decision":"ship staging before production"}'
{"event_seq": 2, "revision": 2}

$ orda-state renew --session demo --ttl 300
{ "acquired_utc": "2026-09-18T14:41:18Z", "epoch": 1,
  "expires_utc": "2026-09-18T14:46:19Z", "holder": "demo",
  "note": "guide verification session", "renew_utc": "2026-09-18T14:41:19Z",
  "session": "demo", "ttl_s": 300 }

$ orda-state brief --format md
# Orda Live Brief (projection — generated; do not hand-edit)

revision 2 | epoch 1 | seat demo (expires 2026-09-18T14:46:19Z) | last writer demo

## RUNNING
- example-project: Working on the example feature | next: Run integration tests | blockers: needs API token; waiting on review

Detail: events.jsonl (through seq 2) — pointers only, no payload copied.

$ orda-state set-project --session demo --slug example-project \
    --summary "conflicting write" --expected-rev 1
ERROR[3]: revision conflict: expected 1, current 2 (last writer demo)      # exit 3

$ orda-state set-project --session ghost --slug x --status running
ERROR[4]: no live lease for session 'ghost' (claim first; ...)             # exit 4

$ orda-state event --session demo --kind note --data 'not-json'
ERROR[2]: --data must be valid JSON                                        # exit 2

$ orda-state verify
{"events": 2, "last_event_rev": 2, "ok": true, "problems": [], "revision": 2}
```

All outputs above are verbatim from a live run against a scratch home; only
paths are abbreviated. The canonical home verified clean in the same session
(`ok: true`, 117 events, revision 117).

## Appendix B. Test coverage (v1 suite)

`python3 -m unittest discover -s <home>/tool/tests -t <home>/tool/tests` —
verified passing 2026-09-18. Covers: write-requires-lease (exit 4), claim/
renew/release lifecycle, second-session refusal without steal, stale-lease
recovery with epoch bump + takeover event, CAS revision conflict (exit 3,
loser's data rejected), monotonic revisions + hash chain, crash-window
flag-and-repair, projection rebuild, TASK-HOME zero-loss migration +
idempotency + source-untouched, tampered log ⇒ `verify` exit 5,
export/import roundtrip, full CLI flow, and real multi-process concurrency
(shared seat, no lost updates; distinct sessions, explicit takeovers).
