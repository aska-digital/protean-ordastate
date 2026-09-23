# Orda State v2 — git-substrate multi-writer store (`orda2` 2.0.0)

Production cutover from the v1 single-writer store, per the LOCKED
cutover architecture (lane `orda-cutover-v2-20260922`). Stdlib + git only,
no server, no network at runtime. All tests and rehearsals use synthetic
homes under temp dirs; the live store is never touched by this repo's
tooling except through the operator runbook below.

## What it is

- **Home** is a git repo with `main` checked out (`records/`, `events.jsonl`, `state.json`, `projection/brief.{md,json}`, `proposal-conflicts/`, `reviews/`, `seat.json`).
- **Writers** each get a worktree + branch `orda/prop/<session>/<n>`; ingest uses `orda/ingest/<batch>`.
- **Seat** (single merge lease, `seat.json` + flock) is the only writer to `main`: gate → splice → regenerate → commit. Exit codes 0/2/3/4/5 as v1.

## Module layout

```
orda2/           orda2_cli.py, orda2_store.py, orda2_events.py, orda2_projection.py, orda2_migration.py, orda2_ingest.py
tests/           test_concurrency.py, test_conflict.py, test_crash_recovery.py, test_migration.py, test_reader_roundtrip.py, test_ingest.py, test_remediation.py, test_cutover.py
sandbox/         v1-snapshot/ (synthetic 8-record rev-21), fixtures/links25.json (25 items, one malformed, one duplicate), fixtures/v1-100/ (100-record rev-126), demo/run.sh
```

## Reproduce

```sh
# demo — full §11 scenario (3+ writers, one seat, one reader, conflict refused, crash+recovery, ingest, round-trip) from clean clone
bash sandbox/demo/run.sh
# demo writes to a temp home; to use a fixed home:
bash sandbox/demo/run.sh /tmp/my-orda-home

# tests (each spins a temp home, never the live store)
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

## Enforcement boundary (honest)

The prototype has no server; enforcement is CLI-level + git. The filesystem retains OS-level writability — a process that bypasses the CLI entirely writes a git worktree it owns (or directly mutates the main checkout). The defense is:

- Every mutating CLI command (`propose`, `merge`, `ingest`, etc.) re-verifies main is clean (no untracked/modified `records/`, `events/`, `state/`) BEFORE it operates and refuses with an explanatory message when main is dirty - a direct writer's debris cannot be silently worked around.
- `merge` additionally refuses when main has foreign uncommitted changes; the merge seat's pre-merge cleanliness check is the gate that catches bypasses.
- A bypass that never goes through `merge` leaves debris that the next legitimate `verify`/`merge`/`propose` will surface as a dirty-main error, not a silent acceptance.

## Recorded output (this branch)

Tests: `43 passed` (pytest: the 38-test prototype suite plus 5 cutover-gate
tests in `tests/test_cutover.py` — live-shape genesis parity, init-refuses-
non-empty, idempotent re-run, `--version` contract, J13 boundary guard).
Demo: exits 0; final lines include `=== DEMO COMPLETE ===` plus `verify ok`,
`projection matches reconcile`, `export bundle carries no transcript/secrets`,
`git fsck ok`. See `sandbox/demo/run.sh` output for the exact conflict message:

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

## Cutover runbook (v1 → v2, operator lane only)

Cutover itself is an owner decision, executed on the live machine by the
cutover lane — never by this tool unattended. The sequence below is the
locked migration (§5 of the cutover architecture). Store paths are the
operator machine's canonical homes.

```sh
# 1. Freeze point (owner-approved moment). Record v1 verify + manifest:
orda-state verify  # v1 CLI against the live home, must exit 0
sha256sum ~/.hermes/eldunari/nexus/state/orda/state.json \
          ~/.hermes/eldunari/nexus/state/orda/events.jsonl \
          ~/.hermes/eldunari/nexus/state/orda/seat.json > archive/v1-precutover-manifest.json

# 2. Archive the stale rehearsal store — never delete it:
mv ~/.hermes/eldunari/nexus/state/orda2 ~/.hermes/eldunari/nexus/state/orda2-rehearsal-$(date -u +%Y%m%dT%H%M%SZ)

# 3. Fresh genesis from the CURRENT live v1 (init refuses a non-empty home, exit 2):
python3 -m orda2.orda2_cli init --home ~/.hermes/eldunari/nexus/state/orda2 \
    --from-v1 ~/.hermes/eldunari/nexus/state/orda

# 4. Verify (must exit 0):
python3 -m orda2.orda2_cli verify --home ~/.hermes/eldunari/nexus/state/orda2

# 5. Parity proof (Shaka Q1): 183/183 slug-set equality, field-by-field
#    payload equality, event parity 335 + 1 genesis = 336, v1 checksums
#    byte-identical vs the step-1 manifest.
```

Recorded rehearsal output (synthetic v1 replica at the live shape —
335 events / 183 records; the live store was not involved):

```
$ python3 -m orda2.orda2_cli init --home <store> --from-v1 <v1-replica>
{
  "record_count": 183,
  "v1_head_hash": "40a62e66deadbcba05c17d2a1a14e0824fbbd8c3ac55013f1588bd8ff1905af2",
  "v1_revision": 335
}

$ python3 -m orda2.orda2_cli verify --home <store>
{
  "events": 336,
  "ok": true,
  "problems": []
}

$ parity check
slug-set 183/183 equal
field mismatches: 0
v2 events: 336
```

(`<store>` / `<v1-replica>` redact the rehearsal's temp paths; all other
bytes verbatim. Re-running the import over the complete store is a no-op:
`{"already": true, ...}`, events unchanged. A crash mid-import is repaired
by archive-and-rerun — in-place delta merge is not supported.)

## Rollback (one command, whole-cutover undo)

```sh
mv ~/.hermes/eldunari/nexus/state/orda2 ~/.hermes/eldunari/nexus/state/orda2.archived-$(date -u +%Y%m%dT%H%M%SZ) && git -C ~/.hermes/eldunari revert --no-edit <CUTOVER_ELDUNARI_COMMIT>
```

where `<CUTOVER_ELDUNARI_COMMIT>` is the single Eldunari commit adding the
gitignore line + STATE.md pointer. Post-rollback proof: `orda-state verify`
exits 0 and sha256 comparison against the pre-cutover manifest passes.
Consumers resume the v1 command block; v1's seat machinery is intact. Any
v2-only work is re-proposed to v1 by hand (expected zero at cutover).

## Release ordering

The `v2.0.0` annotated tag + visible GitHub release land only after Team
Review merge (a later lane, §M read-back). The release notes must reference
the §J amendment PR on `aska-digital/protean-control-plane`: the release
must not ship while the old single-writer §J law still reads as current —
whether that PR merges before or after is the owner's call.

## Wording amendment. Locked architecture decision record (2026-09-23)

This is a documentation-only clarification to the locked cutover architecture.
It changes no behavior, code, or invariant. During fresh genesis, `rev` is
seeded to the v1 revision that last touched each record. For the three live
records with no provenance event, the fallback is `rev :=` the v1 head revision
value, or `1` when no v1 revision value exists. In the v2 record format, J3 is
therefore: one file per record, `records/<slug>.json`, carrying the v1 payload
plus `rev`, `head_event`, and the additive `writer` field. Genesis, CAS,
conflict semantics, and the parity exclusions for `rev` and `head_event` are
unchanged.
