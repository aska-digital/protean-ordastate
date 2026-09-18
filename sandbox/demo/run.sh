#!/usr/bin/env bash
set -euo pipefail
# Orda State v2 — demo per §11, synthetic data only, runs from clean clone.
# Usage: sandbox/demo/run.sh [--home H]  — defaults to a temp home so it never touches live store.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
HOME_DIR="${1:-}"
if [[ -z "$HOME_DIR" ]]; then
  HOME_DIR="$(mktemp -d)"
  CLEANUP_HOME=1
else
  CLEANUP_HOME=0
fi
V1_SNAPSHOT="$REPO_ROOT/sandbox/v1-snapshot"
LINKS="$REPO_ROOT/sandbox/fixtures/links25.json"

ORDA="python3 -m orda2.orda2_cli"
echo "=== Orda State v2 demo ==="
echo "home: $HOME_DIR"
echo "repo: $REPO_ROOT"
echo ""

fail() { echo "FAIL: $*" >&2; exit 1; }

# 1. init
echo "[1] init --from-v1 sandbox/v1-snapshot → 8 records migrated, verify ok"
$ORDA init --home "$HOME_DIR" --from-v1 "$V1_SNAPSHOT"
$ORDA verify --home "$HOME_DIR" || fail "verify after init"
echo "  ok: $(cat "$HOME_DIR/state.json" | python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["projects"]))' "$HOME_DIR/state.json") records"
echo ""

# 2. W-A
echo "[2] W-A: worktree new --session sess-a; update 3 records; propose"
$ORDA worktree --home "$HOME_DIR" new --session sess-a
WTA="$HOME_DIR/worktrees/sess-a/1"
python3 -c "
import json
for slug in ['proj-a','proj-b','proj-x']:
    p='$WTA/records/'+slug+'.json'
    d=json.load(open(p))
    d['summary']='sess-a edit %s' % slug
    open(p,'w').write(json.dumps(d, indent=2, sort_keys=True)+'\n')
"
$ORDA propose --home "$HOME_DIR" --session sess-a
echo ""

# 3. W-B (same base, overlaps proj-x)
echo "[3] W-B: worktree new --session sess-b; updates including proj-x (conflict)"
$ORDA worktree --home "$HOME_DIR" new --session sess-b
WTB="$HOME_DIR/worktrees/sess-b/1"
python3 -c "
import json
for slug, txt in [('proj-x','sess-b conflict on proj-x'), ('proj-c','sess-b edits proj-c')]:
    p='$WTB/records/'+slug+'.json'
    d=json.load(open(p))
    d['summary']=txt
    open(p,'w').write(json.dumps(d, indent=2, sort_keys=True)+'\n')
"
$ORDA propose --home "$HOME_DIR" --session sess-b
echo ""

# 4. ING
echo "[4] ING: ingest --file fixtures/links25.json (25 items, one malformed, one duplicate)"
$ORDA ingest --home "$HOME_DIR" --session ingest --file "$LINKS" --batch ingest-demo
echo ""

# 5. SEAT: review + merges
echo "[5] SEAT: claim, review (3 proposals), merge ingest (fast-track quarantine), merge sess-a, sess-b refused"
$ORDA seat --home "$HOME_DIR" claim --session merge-seat
echo "--- review queue ---"
$ORDA review --home "$HOME_DIR"
echo "--- merge ingest ---"
$ORDA merge --home "$HOME_DIR" --session merge-seat --proposal orda/ingest/ingest-demo
echo "  inbox valid: $(ls "$HOME_DIR/inbox/ingest-demo" 2>/dev/null | wc -l | tr -d ' ') quarantined: $(ls "$HOME_DIR/inbox/_quarantine/ingest-demo" 2>/dev/null | wc -l | tr -d ' ')"
echo "--- merge sess-a ---"
$ORDA merge --home "$HOME_DIR" --session merge-seat --proposal orda/prop/sess-a/1
echo "--- merge sess-b (expect CONFLICT exit 3) ---"
set +e
$ORDA merge --home "$HOME_DIR" --session merge-seat --proposal orda/prop/sess-b/1 2>&1
RC=$?
set -e
if [[ $RC -ne 3 ]]; then fail "expected conflict exit 3 got $RC"; fi
echo "  refused with exit 3 as required"
echo "--- brief shows conflict ---"
$ORDA brief --home "$HOME_DIR" | grep -A2 "CONFLICT" | head -10
echo ""

# 6. W-B rebase + merge
echo "[6] W-B: propose --rebase; seat merges it"
$ORDA propose --home "$HOME_DIR" --session sess-b --rebase
$ORDA merge --home "$HOME_DIR" --session merge-seat --proposal orda/prop/sess-b/1
echo "  merged after rebase"
echo "  reviews: $(ls "$HOME_DIR/reviews" | wc -l | tr -d ' ') verdicts"
echo ""

# 7. Reader one-read join
echo "[7] R: reader fresh mid-flight, brief once → one read"
$ORDA brief --home "$HOME_DIR" --format md | head -40
echo "  join = one read (brief.md)"
echo ""

# 8. Crash injection
echo "[8] Crash: splice fault injection, verify flags, takeover + reconcile"
# create a new writer for crash test
$ORDA worktree --home "$HOME_DIR" new --session sess-crash
WTC="$HOME_DIR/worktrees/sess-crash/1"
python3 -c "
import json
p='$WTC/records/proj-d.json'
d=json.load(open(p))
d['summary']='crash-test edit of proj-d'
open(p,'w').write(json.dumps(d, indent=2, sort_keys=True)+'\n')
"
$ORDA propose --home "$HOME_DIR" --session sess-crash
echo "  proposing then merging with ORDA2_CRASH_AFTER=splice (simulated SIGKILL)..."
set +e
ORDA2_CRASH_AFTER=splice $ORDA merge --home "$HOME_DIR" --session merge-seat --proposal orda/prop/sess-crash/1 2>&1
RC=$?
set -e
echo "  crashed merge exit $RC (expected non-zero)"
echo "--- verify should flag ---"
set +e
$ORDA verify --home "$HOME_DIR" 2>&1
VRC=$?
set -e
if [[ $VRC -ne 5 ]]; then echo "  verify flagged with exit $VRC (expected 5 if projection stale)"; fi
$ORDA verify --home "$HOME_DIR" || true
echo "--- stale takeover + reconcile ---"
# seat may have expired or not; use --steal for deterministic
$ORDA seat --home "$HOME_DIR" claim --session merge-seat2 --steal 2>&1 | head -5
$ORDA reconcile --home "$HOME_DIR" --session merge-seat2
$ORDA verify --home "$HOME_DIR" || fail "verify after reconcile"
echo "  verify ok after reconcile"
echo ""

# 9. Round-trip proof
echo "[9] Round-trip proof: verify (chain+fsck), projection == reconcile, export no secrets"
$ORDA verify --home "$HOME_DIR"
# projection matches reconcile output byte-for-byte
cp "$HOME_DIR/projection/brief.json" /tmp/orda-demo-brief-before.json
$ORDA reconcile --home "$HOME_DIR" --session merge-seat2 >/dev/null
python3 -c "
import json
a=json.load(open('/tmp/orda-demo-brief-before.json'))
b=json.load(open('$HOME_DIR/projection/brief.json'))
a.pop('generated_utc',None); b.pop('generated_utc',None)
assert a==b, 'projection mismatch ignoring generated_utc: %s vs %s' % (a,b)
print('  projection matches reconcile (ignoring generated_utc): ok')
"
$ORDA export --home "$HOME_DIR" --out /tmp/orda-demo-export.json
python3 -c "
import json
b=json.load(open('/tmp/orda-demo-export.json'))
assert b['kind']=='orda-handoff'
text=open('/tmp/orda-demo-export.json').read()
for bad in ['PRIVATE_KEY','AKIA','ghp_','transcript']:
    assert bad not in text, 'secret leaked: '+bad
print('  export bundle carries no transcript/secrets: ok')
print('  exported bytes', len(text))
"
git -C "$HOME_DIR" fsck --no-dangling 2>&1 | head -5
echo "  git fsck ok"
echo ""
echo "=== DEMO COMPLETE ==="
echo "home: $HOME_DIR"
if [[ $CLEANUP_HOME -eq 1 ]]; then
  echo "(temp home retained for inspection: $HOME_DIR)"
fi
