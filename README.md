# Orda State

Shared continuity, state, and handoff layer for multi-session agent
orchestration. When several agent sessions work on the same projects across
days, models, and restarts, Orda State gives them one canonical, append-only
state home: a writer seat with explicit claiming, an event log with hash-chain
integrity, a compact projection for cheap session starts, and a handoff bundle
that moves the seat between sessions without importing a transcript.

**This repository is documentation only.** The operator guide here describes
the system and how to run it; it is not a state store. The canonical state of
any deployment lives on the machine that runs it, in that deployment's own
state home - never in a copy of this repository.

## Do you need this?

USE WHEN:

- Multiple agent sessions (different models, different days, crashed and
  restarted) must share one evolving picture of projects, decisions, blockers,
  and next actions.
- You need exactly one writer at a time, with unlimited read-only observers,
  and a takeover path that is explicit and recorded.
- Session handoff must be cheap and lossless without re-reading transcripts.

SKIP WHEN:

- A single session owns everything and never hands off - a plain file is
  simpler.
- You need multi-machine coordination. v1 is single-machine (POSIX flock);
  that limit is stated, not hidden. See the guide, section 15.

## Quick start

```sh
# Point the examples at your install (portable placeholders throughout)
export HERMES_HOME="$HOME/.hermes"            # adjust if your install lives elsewhere
export ORDA_HOME="$HERMES_HOME/eldunari/nexus/state/orda"
export ORDA="python3 $ORDA_HOME/tool/orda_state.py"

# 1. One-time: create the state home
$ORDA init

# 2. Every session: pick a unique session id, read the brief, claim the seat
export ORDA_SESSION="my-session-$(date +%Y%m%d-%H%M%S)"
$ORDA brief
$ORDA claim --session "$ORDA_SESSION" --ttl 900

# 3. Do work through the CLI, never by hand-editing state files
$ORDA set-project --session "$ORDA_SESSION" --slug my-project --status active
$ORDA event --session "$ORDA_SESSION" --type decision --data '{"note":"..."}'

# 4. Leave cleanly (or let the lease expire; recovery is documented)
$ORDA release --session "$ORDA_SESSION"
```

## The guide

The full operator guide is [`docs/ORDA-STATE-GUIDE.md`](docs/ORDA-STATE-GUIDE.md)
(a zero-build rendered copy is at
[`docs/ORDA-STATE-GUIDE.html`](docs/ORDA-STATE-GUIDE.html)). Every command,
exit code, and limit in it was verified against the live CLI and the v1 test
suite; statements about planned behavior are marked explicitly. It covers:

- what Orda State solves and what it does not;
- canonical state location and file layout;
- first-time setup and session start (brief, session id, seat claim);
- writer-seat semantics: one writer, unlimited readers, claim/renew/release,
  stale recovery, explicit `--steal`, recorded takeovers;
- TTL behavior and crashed-session recovery;
- all CLI commands with examples and the exit-code table (0/2/3/4/5) with
  recovery actions;
- concurrency internals: CAS revision conflicts, flock, atomic writes,
  hash-chain integrity, reconcile;
- session handoff between models/sessions without transcript import;
- what belongs in state versus local secrets, logs, caches, and scratch;
- security and privacy rules, backup, verification, rollback, and disaster
  recovery;
- known limits of the v1 contract and a troubleshooting matrix.

## Security boundary

The state is local-machine scope. Credentials, tokens, transcripts, logs,
caches, and scratch never belong in the state, the events, the projections, or
a handoff bundle. State files are written with restrictive permissions. Session
ids are bearer credentials for the seat: pick unguessable ids and do not share
them. The guide's section 14 is the authoritative statement.

## Relationship to the Protean kit

Orda State is the continuity layer beneath a multi-agent team's procedure
layer. The meeting/decision procedure (councils) and the dispatch control
plane that reference this state system are documented in the
[protean-control-plane](https://github.com/aska-digital/protean-control-plane)
repository; this repository is the state system's own home and the guide's
canonical location.

## Version and status

- Guide and contract: **v1** (schema_version 1, locked 2026-09-18). Verified
  against the live CLI and the 18-test suite on 2026-09-18.
- Planned, not implemented: chain-preserving event-log compaction, migration
  of dispatch flows off the legacy task-home file, multi-machine coordination.
  These are labeled as future work in the guide; nothing here claims them.

## License

MIT. The committed `LICENSE` file is authoritative.
