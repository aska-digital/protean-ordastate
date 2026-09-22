"""Cutover acceptance tests (Leo D3/D7: M2/M3, J13, live-shape genesis).

All homes are temp dirs. The live v1 store is never touched.
"""
import json
import os
import shutil
import subprocess
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Live shape at lock time (leo-architecture §5): 335 events / 183 records.
LIVE_EVENTS = 335
LIVE_RECORDS = 183

PARITY_FIELDS = ("detail_ref", "last_writer", "slug", "status", "summary", "updated_utc")


def run_cli(*args):
    return subprocess.run(
        ["python3", "-m", "orda2.orda2_cli"] + list(args),
        capture_output=True, text=True, cwd=REPO_ROOT)


def build_v1_shape(path, n_records=LIVE_RECORDS, n_events=LIVE_EVENTS):
    """Deterministic synthetic v1 home: n_events chained, n_records slugs."""
    from orda2.orda2_events import event_hash, ZERO_HASH
    os.makedirs(path, exist_ok=True)
    slugs = ["lane-%03d" % i for i in range(n_records)]
    projects = {}
    for i, slug in enumerate(slugs):
        projects[slug] = {
            "slug": slug,
            "status": "running",
            "summary": "record %s" % slug,
            "detail_ref": "ref/%s" % slug,
            "last_writer": "writer-init",
            "updated_utc": "2026-09-01T00:00:00Z",
        }
    events = []
    prev = ZERO_HASH
    last_touch = {}
    for seq in range(1, n_events + 1):
        slug = slugs[(seq - 1) % n_records] if seq > n_records else slugs[seq - 1]
        if seq <= n_records:
            data = {"status": "running", "summary": "record %s" % slug,
                    "detail_ref": "ref/%s" % slug}
        else:
            data = {"status": "closed", "summary": "record %s updated at %d" % (slug, seq)}
            projects[slug].update({"status": "closed",
                                   "summary": data["summary"],
                                   "last_writer": "writer-late",
                                   "updated_utc": "2026-09-02T00:00:00Z"})
        ev = {"seq": seq, "prev_hash": prev, "revision": seq,
              "ts": "2026-09-02T00:00:00Z", "writer": "writer-w",
              "kind": "project-upsert", "project": slug, "data": data,
              "note": "sync"}
        ev["hash"] = event_hash(prev, ev)
        events.append(ev)
        prev = ev["hash"]
        last_touch[slug] = seq
    with open(os.path.join(path, "events.jsonl"), "w") as f:
        for e in events:
            f.write(json.dumps(e, sort_keys=True) + "\n")
    state = {"schema_version": 1, "revision": n_events, "epoch": 1,
             "updated_utc": "2026-09-02T00:00:00Z", "projects": projects}
    with open(os.path.join(path, "state.json"), "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    with open(os.path.join(path, "seat.json"), "w") as f:
        json.dump({"session": None, "epoch": 1, "note": "released"}, f)
    return {"head_hash": prev, "last_touch": last_touch}


def fresh_home(base):
    home = os.path.join(base, "store")
    return home


def test_live_shape_genesis_parity():
    """D3/M2: fresh genesis from a live-shape v1 replica is complete."""
    tmp = tempfile.mkdtemp()
    try:
        v1 = os.path.join(tmp, "v1")
        meta = build_v1_shape(v1)
        home = fresh_home(tmp)
        r = run_cli("init", "--home", home, "--from-v1", v1)
        assert r.returncode == 0, r.stderr + r.stdout
        # event parity: 335 + 1 genesis
        v2_events = [json.loads(l) for l in open(os.path.join(home, "events.jsonl"))]
        assert len(v2_events) == LIVE_EVENTS + 1
        genesis = v2_events[-1]
        assert genesis["kind"] == "genesis"
        assert genesis["data"]["v1_revision"] == LIVE_EVENTS
        assert genesis["data"]["v1_event_count"] == LIVE_EVENTS
        assert genesis["data"]["record_count"] == LIVE_RECORDS
        assert genesis["data"]["v1_head_hash"] == meta["head_hash"]
        # record parity: 183/183 slug-set equality
        rec_dir = os.path.join(home, "records")
        v2_slugs = set(f[:-5] for f in os.listdir(rec_dir) if f.endswith(".json"))
        v1_state = json.load(open(os.path.join(v1, "state.json")))
        assert v2_slugs == set(v1_state["projects"].keys())
        assert len(v2_slugs) == LIVE_RECORDS
        # field-by-field payload equality (v2-only rev/head_event excluded)
        state = json.load(open(os.path.join(home, "state.json")))
        for slug, v1rec in v1_state["projects"].items():
            v2rec = state["projects"][slug]
            for k in PARITY_FIELDS:
                assert v1rec.get(k) == v2rec.get(k), (slug, k)
        # per-record rev = v1 revision that last touched the record
        for slug in ("lane-000", "lane-100", "lane-182"):
            rec = json.load(open(os.path.join(rec_dir, "%s.json" % slug)))
            assert rec["rev"] == meta["last_touch"][slug], slug
            assert rec["head_event"]  # anchored to the touching event
        # verify passes
        r = run_cli("verify", "--home", home)
        assert r.returncode == 0, r.stdout + r.stderr
        assert json.loads(r.stdout)["ok"] is True
        # v1 replica untouched
        assert len(open(os.path.join(v1, "events.jsonl")).readlines()) == LIVE_EVENTS
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_init_refuses_nonempty_home():
    """M2: init --from-v1 REFUSES a non-empty target (exit 2, archive step)."""
    tmp = tempfile.mkdtemp()
    try:
        v1 = os.path.join(tmp, "v1")
        build_v1_shape(v1, n_records=8, n_events=21)
        # case 1: foreign junk dir
        home = os.path.join(tmp, "junk")
        os.makedirs(home)
        open(os.path.join(home, "stale.txt"), "w").write("rehearsal debris")
        r = run_cli("init", "--home", home, "--from-v1", v1)
        assert r.returncode == 2, r.stdout + r.stderr
        assert "rehearsal" in r.stderr and "mv " in r.stderr, r.stderr
        assert open(os.path.join(home, "stale.txt")).read() == "rehearsal debris"
        # runbook path: archive-and-rerun succeeds
        os.rename(home, home + ".rehearsal-TEST")
        r = run_cli("init", "--home", home, "--from-v1", v1)
        assert r.returncode == 0, r.stderr + r.stdout
        # case 2: complete store from a DIFFERENT source is refused, not merged
        v1b = os.path.join(tmp, "v1b")
        build_v1_shape(v1b, n_records=8, n_events=22)  # one event ahead: different head
        r = run_cli("init", "--home", home, "--from-v1", v1b)
        assert r.returncode == 2, r.stdout + r.stderr
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_init_idempotent_same_source():
    """D3 §6: re-running the import over a complete store is a no-op."""
    tmp = tempfile.mkdtemp()
    try:
        v1 = os.path.join(tmp, "v1")
        build_v1_shape(v1, n_records=8, n_events=21)
        home = fresh_home(tmp)
        r = run_cli("init", "--home", home, "--from-v1", v1)
        assert r.returncode == 0, r.stderr + r.stdout
        before = open(os.path.join(home, "events.jsonl")).read()
        r = run_cli("init", "--home", home, "--from-v1", v1)
        assert r.returncode == 0, r.stderr + r.stdout
        assert "already" in r.stdout
        after = open(os.path.join(home, "events.jsonl")).read()
        assert before == after
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_version_contract():
    """M3: --version carries package version, store model, exit-code contract."""
    r = run_cli("--version")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "orda2 2.0.0 (git-substrate multi-writer; exit codes 0/2/3/4/5)"


def test_j13_boundary_guard():
    """J13: verify fails when an enclosing repo sees the store path."""
    tmp = tempfile.mkdtemp()
    try:
        # enclosing host repo (stands in for Eldunari)
        subprocess.run(["git", "init", "-b", "main"], cwd=tmp,
                       capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "t@local"], cwd=tmp,
                       capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=tmp,
                       capture_output=True, check=True)
        v1 = os.path.join(tmp, "v1src")
        build_v1_shape(v1, n_records=4, n_events=10)
        home = os.path.join(tmp, "state", "orda2")
        r = run_cli("init", "--home", home, "--from-v1", v1)
        assert r.returncode == 0, r.stderr + r.stdout
        # enclosing repo sees the store path -> verify exit 5
        r = run_cli("verify", "--home", home)
        assert r.returncode == 5, r.stdout + r.stderr
        assert "J13" in r.stdout
        # the ignore rule (cutover commit) restores the boundary
        with open(os.path.join(tmp, ".gitignore"), "w") as f:
            f.write("state/orda2/\n")
        r = run_cli("verify", "--home", home)
        assert r.returncode == 0, r.stdout + r.stderr
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
