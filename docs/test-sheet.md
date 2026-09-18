# Owner test sheet: v1.0 and v1.0.1 beta

Two tracks. v1.0 is the safe track. v1.0.1 beta is the test track. The beta is not stable.

Setup (one time). The `orda2` command is already installed on this machine, so there is nothing to clone and no `--home` to pass — type the commands below from any directory:

```
orda2 --version                             # prints: orda2 1.0.1-beta
```

Its state home defaults to `~/.hermes/eldunari/nexus/state/orda2` (the beta store).

Fallback, only if `orda2: command not found` — run this from inside a clone:

```
git clone https://github.com/aska-digital/protean-ordastate.git ~/protean-ordastate
cd ~/protean-ordastate
python3 -m orda2.orda2_cli brief --home ~/.hermes/eldunari/nexus/state/orda2
```

Every command in this sheet has the same fallback form: `python3 -m orda2.orda2_cli <subcommand> ... --home ~/.hermes/eldunari/nexus/state/orda2`. Keep `--home` AFTER the subcommand — the CLI ignores a `--home` written before it.

Note: after the marking change lands, EVERY orda2 command also prints one stderr line first before its normal output:

`orda2 v1.0.1-beta (beta - owner test track; the safe track is v1.0 orda-state)`

That line is expected. It is not an error. (`orda2 --version` and `orda2 --help` are the exception: argparse exits before that line, so they print no banner.)

## 1. Read commands, side by side

Both read only. Neither changes anything. Both exit 0.

v1.0.1 beta (home already initialized, 177 events):

```
orda2 brief
```

v1.0 (never modified by the beta work):

```
python3 ~/.hermes/eldunari/nexus/state/orda/tool/orda_state.py brief
```

## 2. Write one real thing (beta track only)

Home for every command below: `~/.hermes/eldunari/nexus/state/orda2` (the `orda2` default, so no `--home` is needed). Run in this order:

```
orda2 seat claim --session OWNER-TEST-1 --ttl 900
orda2 worktree new --session OWNER-TEST-1
```

Then a batch file, e.g. /tmp/owner-probe.json, containing:

```
{"records": [{"slug": "owner-probe-1", "status": "active", "summary": "first beta write by owner"}]}
```

Then:

```
orda2 propose --session OWNER-TEST-1 --file /tmp/owner-probe.json
orda2 merge --session OWNER-TEST-1 --proposal orda/prop/OWNER-TEST-1/1
orda2 verify
```

Good result: propose prints JSON with "branch": "orda/prop/OWNER-TEST-1/1", "events": 1, and a manifest block whose "event_count" is 1; merge prints `{"events": 1, "merged": "orda/prop/OWNER-TEST-1/1", "revision": <n>}`; then verify prints `{"ok": true, ...}`.

Refusals (all on stderr, with the exit code shown):

- second session while a seat is live (exit 4):

`ERROR[4]: seat held by <session> until <time> (use --steal to force a recorded takeover)`

- merge without holding the seat (exit 4):

`ERROR[4]: no live merge seat for session '<s>' ...`

- token-shaped value in a record (exit 2):

`ERROR[2]: secrets scan hit in records/<slug>.json: pattern ... — value redacted (refused)`

- bad slug (exit 2):

`ERROR[2]: invalid slug in batch file: '<slug>'`

## 3. Five cases, in order

1. Normal write
Run: the full WRITE sequence above.
Should see: the good-result chain (propose branch + event_count 1, merge events 1, verify ok:true).
Fault looks like: any ERROR, or verify reporting ok:false / problems non-empty.

2. Write while another write is in flight
Run: start propose in terminal 1; while it runs (or immediately after, before merge) run merge from terminal 2 with a DIFFERENT session id.
Should see: the second session refused with ERROR[4].
Fault looks like: the second session succeeding without the seat.

3. Two sessions writing at the same time
Run: session A claims the seat, then session B claims.
Should see: B refused with ERROR[4] naming A's session id and expiry.
Fault looks like: both claims succeeding.

4. Interrupted command
Run: start `orda2 merge`, press Ctrl-C mid-run, then run `orda2 verify`.
Should see: verify either ok:true, or a documented recovery message (reconcile).
Fault looks like: verify reporting ok:false with a problem that reconcile does not describe, or a seat/proposal stuck with no way to see it.
Recovery: `orda2 reconcile --session <s>`, then commit the rebuilt files — `git -C ~/.hermes/eldunari/nexus/state/orda2 commit -am reconcile` — because reconcile rebuilds state.json and the projection but does not commit them, and without that commit the next write refuses with `ERROR[2]: main is dirty ... (discard or commit debris then retry)`. Also check `orda2 conflicts` (an empty queue prints `(no conflicts)`).
Note: merge finishes in well under a second on a 177-event store, so a Ctrl-C can land after it has already committed — that is the "verify ok:true" branch, not a fault. To retry the interrupted state, make a fresh proposal first (`orda2 worktree new --session <s>`).

5. Malformed / secret-bearing write
Run: propose a record with an sk-style token, and one with a slug containing a space.
Should see: both refused with ERROR[2], nothing written (no propose output, no branch created).
Fault looks like: any of them landing in the store.

## 4. Rollback (safe track only)

To go back to the safe track at any time, use only the v1.0 CLI:

```
python3 ~/.hermes/eldunari/nexus/state/orda/tool/orda_state.py brief
```

The safe track was never modified by the beta work — same files, same revision numbering, nothing renamed. Rollback loses nothing; stop using the beta commands and everything that mattered is still in v1.0. (Reads on the beta track stay available for comparison.)

## 5. Report (3 lines per command)

```
command: <exact command run>
saw: <what appeared, incl. exit code>
expected: <what the sheet said should happen>
```
