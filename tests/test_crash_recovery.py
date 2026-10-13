
import json, os, subprocess, tempfile, time, hashlib, shutil, datetime
def run(args, cwd=None, env=None):
    import subprocess
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, env=env)
def make_home(tmp, from_v1=None):
    import tempfile, subprocess, os
    home = tempfile.mkdtemp(dir=tmp)
    cmd = ["python3", "-m", "orda2.orda2_cli", "init", "--home", home]
    if from_v1:
        cmd += ["--from-v1", from_v1]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr + r.stdout
    return home
V1_SNAPSHOT = os.path.join(os.path.dirname(__file__), "../sandbox/v1-snapshot")
FIXTURES_LINKS = os.path.join(os.path.dirname(__file__), "../sandbox/fixtures/links25.json")
V1_100 = os.path.join(os.path.dirname(__file__), "../sandbox/fixtures/v1-100")

def test_crash_recovery():
    """T3: kill at splice/records-move/projection; verify flags; reconcile repairs."""
    for stage in ["splice","records-move","projection"]:
        tmp=tempfile.mkdtemp()
        try:
            home=make_home(tmp, V1_SNAPSHOT)
            r=run(["python3","-m","orda2.orda2_cli","worktree","--home",home,"new","--session","sess-a"]); assert r.returncode==0
            wta=os.path.join(home,"worktrees","sess-a","1")
            rec=json.load(open(os.path.join(wta,"records","proj-a.json"))); rec["summary"]="crash test %s" % stage; open(os.path.join(wta,"records","proj-a.json"),"w").write(json.dumps(rec,indent=2,sort_keys=True)+"\n")
            r=run(["python3","-m","orda2.orda2_cli","propose","--home",home,"--session","sess-a"]); assert r.returncode==0
            r=run(["python3","-m","orda2.orda2_cli","seat","--home",home,"claim","--session","merge-seat"]); assert r.returncode==0
            env=dict(os.environ); env["ORDA2_CRASH_AFTER"]=stage
            r=run(["python3","-m","orda2.orda2_cli","merge","--home",home,"--session","merge-seat","--proposal","orda/prop/sess-a/1"], env=env)
            # should crash (non-zero)
            assert r.returncode!=0, f"stage {stage} should have crashed"
            r=run(["python3","-m","orda2.orda2_cli","verify","--home",home])
            # verify should flag problem (stale projection or revision)
            assert r.returncode==5, f"stage {stage} verify should flag 5 got {r.returncode} {r.stdout}"
            # stale takeover + reconcile
            r=run(["python3","-m","orda2.orda2_cli","seat","--home",home,"claim","--session","new-seat","--steal"])
            # may succeed or need wait for ttl; we use steal
            assert r.returncode==0, r.stderr
            r=run(["python3","-m","orda2.orda2_cli","reconcile","--home",home,"--session","new-seat"])
            assert r.returncode==0, r.stderr
            r=run(["python3","-m","orda2.orda2_cli","verify","--home",home])
            assert r.returncode==0, f"after reconcile stage {stage} verify failed: {r.stdout} {r.stderr}"
            assert json.loads(r.stdout)["ok"] is True
            # zero lost events: compare event count >= genesis+proposed
            events=open(os.path.join(home,"events.jsonl")).read().strip().split("\n")
            assert len(events) >= 2, events
            recovered = json.load(open(os.path.join(home, "records", "proj-a.json")))
            assert recovered["summary"] == "crash test %s" % stage
            assert json.load(open(os.path.join(home, "state.json")))["projects"]["proj-a"] == recovered
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# Regression for #9: verify full payloads, not just surviving event counts.
import sys
import pytest
from pathlib import Path
from orda2.orda2_store import Store


def cli(home, *args, stage=None):
    env = dict(os.environ)
    env.pop("ORDA2_CRASH_AFTER", None)
    if stage:
        env["ORDA2_CRASH_AFTER"] = stage
    return run([sys.executable, "-m", "orda2.orda2_cli", args[0], "--home", str(home), *args[1:]], env=env)


def proposal(home, tmp, session, records):
    r = cli(home, "worktree", "new", "--session", session)
    assert r.returncode == 0, r.stderr
    branch = json.loads(r.stdout)["branch"]
    batch = tmp / (session + ".json")
    batch.write_text(json.dumps(records))
    r = cli(home, "propose", "--session", session, "--file", str(batch))
    assert r.returncode == 0, r.stderr
    return branch


def setup_recovery(tmp_path):
    home = tmp_path / "store"
    r = cli(home, "init")
    assert r.returncode == 0, r.stderr
    assert cli(home, "seat", "claim", "--session", "seat").returncode == 0
    records = [{"slug": "existing-record", "summary": "old", "custom": {"old": [1, False]}}]
    branch = proposal(home, tmp_path, "seed", records)
    assert cli(home, "merge", "--session", "seat", "--proposal", branch).returncode == 0
    return home


def expected_targets(home):
    from orda2.orda2_recovery import published, latest_targets
    store = Store(str(home))
    return {p: json.loads(t) for p, (_, t, _) in latest_targets(published(str(home), store.read_events())).items()}


def assert_exact(home, expected):
    state = json.loads((home / "state.json").read_text())
    events = Store(str(home)).read_events()
    assert state["revision"] == events[-1]["revision"]
    for path, rec in expected.items():
        assert json.loads((home / path).read_text()) == rec
        assert state["projects"][rec["slug"]] == rec
        matching = [e for e in events if e.get("project") == rec["slug"] and e["kind"] == "update"][-1]
        assert rec["head_event"] == matching["hash"]
        assert rec["rev"] == matching["revision"]
    assert cli(home, "verify").returncode == 0


@pytest.mark.parametrize("stage", ["splice", "record-file-1", "records-move", "projection"])
def test_exact_payload_recovery_and_next_same_record_update(tmp_path, stage):
    home = setup_recovery(tmp_path)
    before_events = (home / "events.jsonl").read_bytes()
    records = [{"slug": "existing-record", "summary": "changed " + stage,
                "custom": {"deep": [None, True, {"unicode": "हैदराबाद"}]}, "removed": None},
               {"slug": "new-record", "summary": "new", "arbitrary": [1, 2, {"more": 3}]}]
    branch = proposal(home, tmp_path, "writer", records)
    r = cli(home, "merge", "--session", "seat", "--proposal", branch, stage=stage)
    assert r.returncode == 1, r.stderr
    assert (home / "events.jsonl").read_bytes().startswith(before_events)
    assert cli(home, "verify").returncode == 5
    expected = expected_targets(home)
    for rec in records:
        target = expected["records/" + rec["slug"] + ".json"]
        for key, value in rec.items():
            assert target[key] == value
    assert cli(home, "reconcile").returncode == 0
    assert_exact(home, expected)
    event_bytes = (home / "events.jsonl").read_bytes()
    snapshot = (home / "state.json").read_bytes()
    assert cli(home, "reconcile").returncode == 0
    assert (home / "events.jsonl").read_bytes() == event_bytes
    assert (home / "state.json").read_bytes() == snapshot
    # A second transaction supersedes retained evidence for the same path,
    # including a second crash/reconcile and a clean subsequent proposal.
    second = proposal(home, tmp_path, "second", [{"slug": "existing-record", "summary": "second", "custom": {"second": True}}])
    assert cli(home, "merge", "--session", "seat", "--proposal", second, stage="splice").returncode == 1
    expected = expected_targets(home)
    assert cli(home, "reconcile").returncode == 0
    assert_exact(home, expected)
    third = proposal(home, tmp_path, "third", [{"slug": "existing-record", "summary": "third"}])
    assert cli(home, "merge", "--session", "seat", "--proposal", third).returncode == 0
    assert_exact(home, expected_targets(home))


@pytest.mark.parametrize("stage", ["before-intent", "intent"])
def test_unpublished_intent_does_not_replay(tmp_path, stage):
    home = setup_recovery(tmp_path)
    old = (home / "records/existing-record.json").read_bytes()
    events = (home / "events.jsonl").read_bytes()
    branch = proposal(home, tmp_path, "writer", [{"slug": "existing-record", "summary": "unpublished"}])
    assert cli(home, "merge", "--session", "seat", "--proposal", branch, stage=stage).returncode == 1
    assert cli(home, "verify").returncode == 0
    assert cli(home, "reconcile").returncode == 0
    assert (home / "records/existing-record.json").read_bytes() == old
    assert (home / "events.jsonl").read_bytes() == events
    assert cli(home, "merge", "--session", "seat", "--proposal", branch).returncode == 0
    assert_exact(home, expected_targets(home))


@pytest.mark.parametrize("damage", ["missing", "corrupt", "payload", "segment", "path", "partial-segment", "unrelated-write"])
def test_bad_recovery_evidence_refuses_without_writes(tmp_path, damage):
    home = setup_recovery(tmp_path)
    branch = proposal(home, tmp_path, "writer", [{"slug": "existing-record", "summary": "must survive"}, {"slug": "new-record", "summary": "new"}])
    assert cli(home, "merge", "--session", "seat", "--proposal", branch, stage="splice").returncode == 1
    identity = Store(str(home)).read_events()[-1]["recovery_id"]
    journal_path = home / "recovery" / (identity + ".json")
    if damage == "missing":
        journal_path.unlink()
    elif damage == "corrupt":
        journal_path.write_text("{")
    elif damage == "partial-segment":
        lines = (home / "events.jsonl").read_text().splitlines()
        (home / "events.jsonl").write_text("\n".join(lines[:-1]) + "\n")
    elif damage == "unrelated-write":
        (home / "records/existing-record.json").write_text('{"slug":"existing-record","summary":"later valid value","rev":900}\n')
    else:
        journal = json.loads(journal_path.read_text())
        if damage == "payload":
            journal["intent"]["files"]["records/existing-record.json"]["proposed"] = '{"slug":"existing-record","summary":"tampered"}'
        elif damage == "path":
            journal["intent"]["files"]["../outside.json"] = journal["intent"]["files"].pop("records/new-record.json")
        else:
            journal["events"][0]["revision"] += 7
        journal_path.write_text(json.dumps(journal))
    before = {p: p.read_bytes() for p in [home / "events.jsonl", home / "state.json", home / "records/existing-record.json"]}
    assert cli(home, "verify").returncode == 5
    r = cli(home, "reconcile")
    assert r.returncode == 5, r.stderr
    assert "Traceback" not in r.stderr
    assert not (home / "records/new-record.json").exists()
    assert all(p.read_bytes() == value for p, value in before.items())


@pytest.mark.parametrize("stage", ["recovery-file-1", "recovery-state", "recovery-projection"])
def test_recovery_interruption_replays_remaining_files(tmp_path, stage):
    home = setup_recovery(tmp_path)
    branch = proposal(home, tmp_path, "writer", [{"slug": "existing-record", "summary": "recover"}, {"slug": "new-record", "summary": "recover new"}])
    assert cli(home, "merge", "--session", "seat", "--proposal", branch, stage="splice").returncode == 1
    expected = expected_targets(home)
    events = (home / "events.jsonl").read_bytes()
    assert cli(home, "reconcile", stage=stage).returncode == 1
    assert cli(home, "verify").returncode == (0 if stage == "recovery-projection" else 5)
    assert cli(home, "reconcile").returncode == 0
    assert_exact(home, expected)
    assert (home / "events.jsonl").read_bytes() == events


def test_legacy_splice_cannot_be_reconciled_into_false_success(tmp_path):
    from orda2.orda2_events import event_hash
    home = tmp_path / "store"
    assert cli(home, "init").returncode == 0
    store = Store(str(home))
    prev = store.read_events()[-1]
    ev = {"seq": prev["seq"] + 1, "revision": prev["revision"] + 1,
          "prev_hash": prev["hash"], "kind": "update", "ts": prev["ts"],
          "writer": "legacy", "project": "lost-record", "data": {"summary": "missing"}}
    ev["hash"] = event_hash(prev["hash"], ev)
    with (home / "events.jsonl").open("a") as f:
        f.write(json.dumps(ev) + "\n")
    before = (home / "state.json").read_bytes()
    assert cli(home, "verify").returncode == 5
    assert cli(home, "reconcile").returncode == 5
    assert (home / "state.json").read_bytes() == before
    # Simulate the old false reconciliation (revision raised, payload absent).
    state = json.loads(before)
    state["revision"] = ev["revision"]
    (home / "state.json").write_text(json.dumps(state))
    assert cli(home, "verify").returncode == 5
    assert cli(home, "reconcile").returncode == 5


def test_historical_crlf_bytes_are_preserved(tmp_path):
    home = setup_recovery(tmp_path)
    old = (home / "events.jsonl").read_bytes().replace(b"\n", b"\r\n")
    (home / "events.jsonl").write_bytes(old)
    assert run(["git", "add", "events.jsonl"], cwd=home).returncode == 0
    assert run(["git", "commit", "-m", "synthetic CRLF event serialization"], cwd=home).returncode == 0
    branch = proposal(home, tmp_path, "writer", [{"slug": "existing-record", "summary": "after CRLF"}])
    assert cli(home, "merge", "--session", "seat", "--proposal", branch, stage="splice").returncode == 1
    assert (home / "events.jsonl").read_bytes().startswith(old)
    assert cli(home, "reconcile").returncode == 0
    assert (home / "events.jsonl").read_bytes().startswith(old)
    assert_exact(home, expected_targets(home))


def test_reconcile_refuses_unrelated_staged_path_before_replay(tmp_path):
    home = setup_recovery(tmp_path)
    branch = proposal(home, tmp_path, "writer", [{"slug": "existing-record", "summary": "recover"}])
    assert cli(home, "merge", "--session", "seat", "--proposal", branch, stage="splice").returncode == 1
    unrelated = home / "unrelated.txt"
    unrelated.write_text("synthetic foreign staged file\n")
    assert run(["git", "add", "unrelated.txt"], cwd=home).returncode == 0
    old_record = (home / "records/existing-record.json").read_bytes()
    old_head = run(["git", "rev-parse", "HEAD"], cwd=home).stdout
    old_index = run(["git", "diff", "--cached", "--binary"], cwd=home).stdout
    r = cli(home, "reconcile")
    assert r.returncode == 5 and "unrelated staged" in r.stderr
    assert (home / "records/existing-record.json").read_bytes() == old_record
    assert run(["git", "rev-parse", "HEAD"], cwd=home).stdout == old_head
    assert run(["git", "diff", "--cached", "--binary"], cwd=home).stdout == old_index
    assert unrelated.read_text() == "synthetic foreign staged file\n"
    assert run(["git", "reset", "HEAD", "--", "unrelated.txt"], cwd=home).returncode == 0
    assert cli(home, "reconcile").returncode == 0
    assert run(["git", "show", "HEAD:unrelated.txt"], cwd=home).returncode != 0
    assert unrelated.read_text() == "synthetic foreign staged file\n"
    assert_exact(home, expected_targets(home))


@pytest.mark.parametrize("failed_stage", ["intent", "record", "projection"])
def test_io_failure_before_or_after_publication(tmp_path, monkeypatch, failed_stage):
    import orda2.orda2_cli as command
    import orda2.orda2_recovery as recovery
    import orda2.orda2_projection as projection
    from argparse import Namespace
    home = setup_recovery(tmp_path)
    branch = proposal(home, tmp_path, "writer", [{"slug": "existing-record", "summary": "io failure"}, {"slug": "new-record", "summary": "new"}])
    old_events = (home / "events.jsonl").read_bytes()
    with monkeypatch.context() as patch:
        if failed_stage == "intent":
            patch.setattr(recovery, "atomic_write", lambda *args: (_ for _ in ()).throw(OSError("synthetic intent fsync failure")))
        elif failed_stage == "record":
            original = command.atomic_write
            def faulty(path, text):
                if str(path).endswith("records/new-record.json"):
                    raise OSError("synthetic record rename failure")
                return original(path, text)
            patch.setattr(command, "atomic_write", faulty)
        else:
            patch.setattr(projection, "write_projection", lambda *args: (_ for _ in ()).throw(OSError("synthetic projection failure")))
        with pytest.raises(OSError):
            command.cmd_merge(Namespace(home=str(home), session="seat", proposal=branch))
    if failed_stage == "intent":
        assert (home / "events.jsonl").read_bytes() == old_events
        assert cli(home, "verify").returncode == 0
    else:
        assert cli(home, "verify").returncode == 5
        assert cli(home, "reconcile").returncode == 0
        assert_exact(home, expected_targets(home))


def test_reconcile_stages_only_validated_targets_and_evidence(tmp_path):
    home = setup_recovery(tmp_path)
    branch = proposal(home, tmp_path, "writer", [{"slug": "existing-record", "summary": "recover"}])
    assert cli(home, "merge", "--session", "seat", "--proposal", branch, stage="splice").returncode == 1
    unknown_inbox = home / "inbox/unrelated.json"
    unknown_intent = home / "recovery/unknown.json"
    unknown_inbox.write_text('{"unrelated":"synthetic file"}\n')
    unknown_intent.write_text('{"unrelated":"unknown evidence"}\n')
    assert cli(home, "reconcile").returncode == 0
    for path in [unknown_inbox, unknown_intent]:
        assert path.exists()
        assert run(["git", "show", "HEAD:" + str(path.relative_to(home))], cwd=home).returncode != 0
    assert_exact(home, expected_targets(home))


def test_prepared_orphan_intent_stays_uncommitted(tmp_path):
    home = setup_recovery(tmp_path)
    branch = proposal(home, tmp_path, "writer", [{"slug": "existing-record", "summary": "unpublished"}])
    known = set((home / "recovery").glob("*.json"))
    assert cli(home, "merge", "--session", "seat", "--proposal", branch, stage="intent").returncode == 1
    orphan = (set((home / "recovery").glob("*.json")) - known).pop()
    assert cli(home, "reconcile").returncode == 0
    assert run(["git", "show", "HEAD:" + str(orphan.relative_to(home))], cwd=home).returncode != 0
    assert orphan.exists()
    assert cli(home, "merge", "--session", "seat", "--proposal", branch).returncode == 0
    # The successful transaction has a different timestamp/identity if retried
    # later; if it reused the identical intent it is now published evidence.
    referenced = {e.get("recovery_id") for e in Store(str(home)).read_events()}
    if orphan.stem not in referenced:
        assert run(["git", "show", "HEAD:" + str(orphan.relative_to(home))], cwd=home).returncode != 0


def test_state_payload_verification_distinguishes_boolean_from_number(tmp_path):
    home = setup_recovery(tmp_path)
    path = home / "state.json"
    state = json.loads(path.read_text())
    # Python dict equality conflates False with 0; JSON payloads must not.
    state["projects"]["existing-record"]["custom"]["old"][1] = 0
    path.write_text(json.dumps(state))
    before = path.read_bytes()
    assert cli(home, "verify").returncode == 5
    assert cli(home, "reconcile").returncode == 5
    assert path.read_bytes() == before


def test_takeover_uses_durable_tail_after_multirecord_splice(tmp_path):
    home = setup_recovery(tmp_path)
    branch = proposal(home, tmp_path, "writer", [{"slug": "existing-record", "summary": "recover"}, {"slug": "new-record", "summary": "new"}])
    assert cli(home, "merge", "--session", "seat", "--proposal", branch, stage="splice").returncode == 1
    published_revision = Store(str(home)).read_events()[-1]["revision"]
    assert cli(home, "seat", "claim", "--session", "recovery-seat", "--steal").returncode == 0
    events = Store(str(home)).read_events()
    assert events[-1]["kind"] == "takeover"
    assert events[-1]["revision"] == published_revision + 1
    assert cli(home, "reconcile", "--session", "recovery-seat").returncode == 0
    assert_exact(home, expected_targets(home))
    second = proposal(home, tmp_path, "second", [{"slug": "new-record", "summary": "second"}])
    assert cli(home, "merge", "--session", "recovery-seat", "--proposal", second).returncode == 0
    assert_exact(home, expected_targets(home))
    revisions = [e["revision"] for e in Store(str(home)).read_events()]
    assert all(a < b for a, b in zip(revisions, revisions[1:]))


def test_recovery_accepts_coherent_noncanonical_prior_record_format(tmp_path):
    home = Path(make_home(str(tmp_path), V1_SNAPSHOT))
    assert cli(home, "seat", "claim", "--session", "seat").returncode == 0
    path = home / "records/proj-a.json"
    path.write_text(json.dumps(json.loads(path.read_text()), indent=7))
    assert run(["git", "add", "records/proj-a.json"], cwd=home).returncode == 0
    assert run(["git", "commit", "-m", "synthetic record formatting"], cwd=home).returncode == 0
    assert cli(home, "verify").returncode == 0
    branch = proposal(home, tmp_path, "writer", [{"slug": "proj-a", "summary": "changed", "custom": {"deep": [None, False]}}, {"slug": "new-record", "summary": "new"}])
    assert cli(home, "merge", "--session", "seat", "--proposal", branch, stage="record-file-1").returncode == 1
    assert cli(home, "reconcile").returncode == 0
    assert_exact(home, expected_targets(home))
