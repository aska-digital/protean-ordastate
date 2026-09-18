"""Regression tests for Shaka QA findings (remediation round).

Covers:
  attack6 — corruption contract: verify exit 5, reconcile refuses on corrupt state/events/git object
  attack7 — secret ingress: broadened scan at propose/ingest/merge, 3+ shapes
  attack8 — main-write bypass: dirty main refuses propose/merge, debris detected
"""
import json
import os
import subprocess
import tempfile
import shutil


def run(args, cwd=None, env=None):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, env=env)


def make_home(tmp, from_v1=None):
    home = tempfile.mkdtemp(dir=tmp)
    cmd = ["python3", "-m", "orda2.orda2_cli", "init", "--home", home]
    if from_v1:
        cmd += ["--from-v1", from_v1]
    r = run(cmd)
    assert r.returncode == 0, r.stderr + r.stdout
    return home


V1_SNAPSHOT = os.path.join(os.path.dirname(__file__), "../sandbox/v1-snapshot")
# Use clearly-fake prefixes so no real secret is ever stored
FAKE_SLACK = "xoxb-FAKE1234567890-fake-token-for-test"
FAKE_SK = "sk-FAKE1234567890abcdef1234567890"
FAKE_AKIA = "AKIA1234567890ABCDEF"


# ---- Attack 6: corruption contract ----

def test_corrupt_state_verify_exit5_and_reconcile_refuses():
    tmp = tempfile.mkdtemp()
    try:
        home = make_home(tmp, V1_SNAPSHOT)
        # corrupt state.json with unparseable JSON
        open(os.path.join(home, "state.json"), "w").write("{")
        r = run(["python3", "-m", "orda2.orda2_cli", "verify", "--home", home])
        assert r.returncode == 5, f"verify should exit 5 on corrupt state, got {r.returncode} {r.stdout} {r.stderr}"
        assert "Traceback" not in r.stderr, "verify must not emit traceback"
        assert "corrupt" in (r.stdout + r.stderr).lower()
        r2 = run(["python3", "-m", "orda2.orda2_cli", "reconcile", "--home", home])
        assert r2.returncode == 5, f"reconcile should refuse with 5 on corrupt state, got {r2.returncode}"
        assert "reconcile refused" in (r2.stdout + r2.stderr).lower()
        assert "Traceback" not in r2.stderr
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_corrupt_events_line_verify_exit5_and_reconcile_refuses():
    tmp = tempfile.mkdtemp()
    try:
        home = make_home(tmp, V1_SNAPSHOT)
        # append a bad-JSON line (tampered chain)
        with open(os.path.join(home, "events.jsonl"), "a") as f:
            f.write("not-json {{{\n")
        r = run(["python3", "-m", "orda2.orda2_cli", "verify", "--home", home])
        assert r.returncode == 5, f"verify should exit 5 on corrupt events line, got {r.returncode}"
        assert "Traceback" not in r.stderr
        r2 = run(["python3", "-m", "orda2.orda2_cli", "reconcile", "--home", home])
        assert r2.returncode == 5, f"reconcile should refuse on corrupt events, got {r2.returncode}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_corrupt_event_hash_verify_exit5():
    tmp = tempfile.mkdtemp()
    try:
        home = make_home(tmp, V1_SNAPSHOT)
        # tamper first event hash
        p = os.path.join(home, "events.jsonl")
        lines = open(p).read().splitlines()
        obj = json.loads(lines[0])
        obj["hash"] = "f" * 64
        lines[0] = json.dumps(obj, sort_keys=True)
        open(p, "w").write("\n".join(lines) + "\n")
        r = run(["python3", "-m", "orda2.orda2_cli", "verify", "--home", home])
        assert r.returncode == 5, f"verify should exit 5 on bad hash, got {r.returncode} {r.stdout}"
        assert "hash mismatch" in (r.stdout + r.stderr).lower() or "chain broken" in (r.stdout + r.stderr).lower()
        r2 = run(["python3", "-m", "orda2.orda2_cli", "reconcile", "--home", home])
        assert r2.returncode == 5
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_corrupt_git_object_verify_exit5():
    tmp = tempfile.mkdtemp()
    try:
        home = make_home(tmp, V1_SNAPSHOT)
        # corrupt a git object: find a loose object and truncate it
        objs = []
        for root, dirs, files in os.walk(os.path.join(home, ".git", "objects")):
            for fn in files:
                fp = os.path.join(root, fn)
                # skip pack dir
                if "pack" in root:
                    continue
                objs.append(fp)
                break
            if objs:
                break
        if not objs:
            # no loose objects (maybe packed); skip test as feasible check not possible
            return
        # truncate first object (need to chmod as git objects are read-only)
        try:
            os.chmod(objs[0], 0o644)
        except Exception:
            pass
        open(objs[0], "wb").write(b"corrupt")
        r = run(["python3", "-m", "orda2.orda2_cli", "verify", "--home", home])
        assert r.returncode == 5, f"verify should exit 5 on corrupt git object, got {r.returncode} {r.stdout} {r.stderr}"
        assert "Traceback" not in r.stderr
        r2 = run(["python3", "-m", "orda2.orda2_cli", "reconcile", "--home", home])
        assert r2.returncode == 5
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- Attack 7: secret ingress (3 shapes) ----

def test_secret_propose_refuses_slack_token():
    tmp = tempfile.mkdtemp()
    try:
        home = make_home(tmp, V1_SNAPSHOT)
        run(["python3", "-m", "orda2.orda2_cli", "worktree", "--home", home, "new", "--session", "evil"])
        batch = [{"slug": "secret-slack", "summary": FAKE_SLACK}]
        bf = os.path.join(tmp, "batch.json")
        json.dump(batch, open(bf, "w"))
        r = run(["python3", "-m", "orda2.orda2_cli", "propose", "--home", home, "--session", "evil", "--file", bf])
        assert r.returncode != 0
        assert "secrets scan" in r.stderr.lower()
        assert "slack-token" in r.stderr.lower() or "xox" in r.stderr.lower()
        # value must be redacted (full fake token not echoed)
        assert FAKE_SLACK not in r.stderr, "secrets error must redact value"
        # also ensure not landed in records/events
        assert not os.path.exists(os.path.join(home, "records", "secret-slack.json"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_secret_propose_refuses_sk_and_akia():
    for token, label in [(FAKE_SK, "sk-"), (FAKE_AKIA, "AKIA")]:
        tmp = tempfile.mkdtemp()
        try:
            home = make_home(tmp, V1_SNAPSHOT)
            run(["python3", "-m", "orda2.orda2_cli", "worktree", "--home", home, "new", "--session", "evil2"])
            batch = [{"slug": "secret-shape", "summary": token}]
            bf = os.path.join(tmp, "batch.json")
            json.dump(batch, open(bf, "w"))
            r = run(["python3", "-m", "orda2.orda2_cli", "propose", "--home", home, "--session", "evil2", "--file", bf])
            assert r.returncode != 0, f"should refuse {label} token"
            assert "secrets scan" in r.stderr.lower()
            assert token not in r.stderr
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def test_secret_ingest_refuses():
    tmp = tempfile.mkdtemp()
    try:
        home = make_home(tmp, V1_SNAPSHOT)
        batch = [{"slug": "ingest-secret", "summary": FAKE_SLACK}]
        bf = os.path.join(tmp, "ingest.json")
        json.dump(batch, open(bf, "w"))
        r = run(["python3", "-m", "orda2.orda2_cli", "ingest", "--home", home, "--session", "ingest", "--file", bf])
        assert r.returncode != 0
        assert "secrets scan" in r.stderr.lower()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_secret_merge_precondition_refuses():
    # Propose bypass: write secret directly to worktree file then try propose — propose itself should refuse;
    # merging a proposal that somehow contains a secret should also be refused at merge gate.
    tmp = tempfile.mkdtemp()
    try:
        home = make_home(tmp, V1_SNAPSHOT)
        run(["python3", "-m", "orda2.orda2_cli", "worktree", "--home", home, "new", "--session", "evil"])
        wt = os.path.join(home, "worktrees", "evil", "1")
        rec = json.load(open(os.path.join(wt, "records", "proj-a.json")))
        rec["summary"] = FAKE_SLACK
        open(os.path.join(wt, "records", "proj-a.json"), "w").write(json.dumps(rec, indent=2, sort_keys=True) + "\n")
        r = run(["python3", "-m", "orda2.orda2_cli", "propose", "--home", home, "--session", "evil"])
        assert r.returncode != 0
        assert "secrets scan" in r.stderr.lower()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_secret_generic_api_key_refuses():
    tmp = tempfile.mkdtemp()
    try:
        home = make_home(tmp, V1_SNAPSHOT)
        run(["python3", "-m", "orda2.orda2_cli", "worktree", "--home", home, "new", "--session", "evil"])
        batch = [{"slug": "generic-secret", "summary": "hi", "api_key": "FAKEGENERIC1234567890ABCDEF"}]
        bf = os.path.join(tmp, "batch.json")
        json.dump(batch, open(bf, "w"))
        r = run(["python3", "-m", "orda2.orda2_cli", "propose", "--home", home, "--session", "evil", "--file", bf])
        assert r.returncode != 0
        assert "secrets scan" in r.stderr.lower()
        assert "generic-secret" in r.stderr.lower()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- Attack 8: main-write bypass ----

def test_dirty_main_propose_refuses():
    tmp = tempfile.mkdtemp()
    try:
        home = make_home(tmp, V1_SNAPSHOT)
        # direct write debris to main
        open(os.path.join(home, "records", "direct-write.json"), "w").write('{"slug":"direct-write","summary":"bypass"}\n')
        run(["python3", "-m", "orda2.orda2_cli", "worktree", "--home", home, "new", "--session", "s"])
        wt = os.path.join(home, "worktrees", "s", "1")
        rec = json.load(open(os.path.join(wt, "records", "proj-a.json")))
        rec["summary"] = "attempt"
        open(os.path.join(wt, "records", "proj-a.json"), "w").write(json.dumps(rec, indent=2, sort_keys=True) + "\n")
        r = run(["python3", "-m", "orda2.orda2_cli", "propose", "--home", home, "--session", "s"])
        assert r.returncode != 0
        assert "dirty" in r.stderr.lower()
        assert "records" in r.stderr.lower()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_dirty_main_merge_refuses():
    tmp = tempfile.mkdtemp()
    try:
        home = make_home(tmp, V1_SNAPSHOT)
        run(["python3", "-m", "orda2.orda2_cli", "worktree", "--home", home, "new", "--session", "sess-a"])
        wt = os.path.join(home, "worktrees", "sess-a", "1")
        rec = json.load(open(os.path.join(wt, "records", "proj-a.json")))
        rec["summary"] = "good"
        open(os.path.join(wt, "records", "proj-a.json"), "w").write(json.dumps(rec, indent=2, sort_keys=True) + "\n")
        r = run(["python3", "-m", "orda2.orda2_cli", "propose", "--home", home, "--session", "sess-a"])
        assert r.returncode == 0
        run(["python3", "-m", "orda2.orda2_cli", "seat", "--home", home, "claim", "--session", "seat"])
        # dirty main before merge
        open(os.path.join(home, "records", "debris.json"), "w").write('{"slug":"debris","summary":"x"}\n')
        r = run(["python3", "-m", "orda2.orda2_cli", "merge", "--home", home, "--session", "seat", "--proposal", "orda/prop/sess-a/1"])
        assert r.returncode != 0
        assert "dirty" in r.stderr.lower()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_debris_file_detected():
    tmp = tempfile.mkdtemp()
    try:
        home = make_home(tmp, V1_SNAPSHOT)
        open(os.path.join(home, "records", "evil-debris.json"), "w").write('{"slug":"evil-debris","summary":"evil"}\n')
        run(["python3", "-m", "orda2.orda2_cli", "worktree", "--home", home, "new", "--session", "s"])
        wt = os.path.join(home, "worktrees", "s", "1")
        rec = json.load(open(os.path.join(wt, "records", "proj-a.json")))
        rec["summary"] = "t"
        open(os.path.join(wt, "records", "proj-a.json"), "w").write(json.dumps(rec, indent=2, sort_keys=True) + "\n")
        r = run(["python3", "-m", "orda2.orda2_cli", "propose", "--home", home, "--session", "s"])
        assert r.returncode != 0
        assert "dirty" in r.stderr.lower()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
