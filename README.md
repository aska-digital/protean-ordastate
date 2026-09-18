# Orda State v2 prototype — git-substrate multi-writer

Prototype per `leo-decision-memo.md` §11 (branch `orda2/prototype`). Stdlib + git only, no server, no network at runtime. All data synthetic under `sandbox/`; live store untouched.

## What it is

- **Home** is a git repo with `main` checked out (`records/`, `events.jsonl`, `state.json`, `projection/brief.{md,json}`, `proposal-conflicts/`, `reviews/`, `seat.json`).
- **Writers** each get a worktree + branch `orda/prop/<session>/<n>`; ingest uses `orda/ingest/<batch>`.
- **Seat** (single merge lease, `seat.json` + flock) is the only writer to `main`: gate → splice → regenerate → commit. Exit codes 0/2/3/4/5 as v1.

## Module layout

```
orda2/           orda2_cli.py, orda2_store.py, orda2_events.py, orda2_projection.py, orda2_migration.py, orda2_ingest.py
tests/           test_concurrency.py, test_conflict.py, test_crash_recovery.py, test_migration.py, test_reader_roundtrip.py, test_ingest.py (also test_orda2.py aggregate)
sandbox/         v1-snapshot/ (synthetic 8-record rev-21), fixtures/links25.json (25 items, one malformed, one duplicate), fixtures/v1-100/ (100-record rev-126), demo/run.sh
```

## Reproduce

```sh
# demo — full §11 scenario (3+ writers, one seat, one reader, conflict refused, crash+recovery, ingest, round-trip) from clean clone
bash sandbox/demo/run.sh
# demo writes to a temp home; to use a fixed home:
bash sandbox/demo/run.sh /tmp/my-orda-home

# tests T1-T6 (each spins a temp home, never the live store)
python3 -m pytest tests/ -v

# individual tests
python3 -m pytest tests/test_concurrency.py tests/test_conflict.py tests/test_crash_recovery.py tests/test_migration.py tests/test_reader_roundtrip.py tests/test_ingest.py -v

# CLI help
python3 -m orda2.orda2_cli --help
```

## CLI (python -m orda2.orda2_cli)

```
init --home H [--from-v1 V1SNAPSHOT]
worktree new --home H --session S
propose --home H --session S [--file BATCH.json] [--rebase]
ingest --home H --session INGEST --file links.json [--batch B]
seat claim|renew|release --home H --session S [--ttl T] [--steal]
review --home H [--wait --timeout T]
merge --home H --session S --proposal P
conflicts --home H
brief --home H [--format md|json]
verify | reconcile --home H [--session S]
export --home H [--out F]
```

Fault injection (crash recovery): `ORDA2_CRASH_AFTER=splice|records-move|projection` before `merge`.

## Recorded output (this branch)

Tests: `6 passed` (pytest). Demo: exits 0; final lines include `=== DEMO COMPLETE ===` plus `verify ok`, `projection matches reconcile`, `export bundle carries no transcript/secrets`, `git fsck ok`. See `sandbox/demo/run.sh` output for the exact conflict message:

```
CONFLICT orda/prop/sess-b/1 vs main — record: proj-x
  proposal base rev: 21 (writer sess-b, ...)
  main rev:          50 (writer sess-a, ...)
  view both:  git show main:records/proj-x.json
              git show orda/prop/sess-b/1:records/proj-x.json
  resolve:    repropose on the newer rev, or decide as reviewer
Refused: same-record concurrent change. Never auto-merged. See brief → Unresolved conflicts. Resolve: orda2 propose --rebase.
```

## What is unproven

- Volume bound: thousands of small JSON files fine for git; GB-scale blobs would need Hazen F-flip #3 (SQLite fallback).
- `rerere` is enabled but not exercised beyond recurring same-record conflict; seat takeover epoch bump is minimal (file + event).
- Projection regeneration is O(events-in-proposal + records) ~ O(100) per merge; full chain `verify` is O(n) with n=~100 in prototype.
