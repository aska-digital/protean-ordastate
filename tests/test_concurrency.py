
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

def test_concurrency():
    """T1: 5 processes propose to disjoint records, all merge, chain intact."""
    tmp = tempfile.mkdtemp()
    try:
        home = make_home(tmp, V1_SNAPSHOT)
        # create 5 worktrees
        for i in range(5):
            r = run(["python3","-m","orda2.orda2_cli","worktree","--home",home,"new","--session",f"sess-{i}"])
            assert r.returncode==0, r.stderr
        # each session edits distinct record
        slugs = ["proj-a","proj-b","proj-c","proj-d","proj-e"]
        for i, slug in enumerate(slugs):
            wt = os.path.join(home,"worktrees",f"sess-{i}","1")
            rec = json.load(open(os.path.join(wt,"records",f"{slug}.json")))
            rec["summary"]=f"concurrent edit {i} on {slug}"
            open(os.path.join(wt,"records",f"{slug}.json"),"w").write(json.dumps(rec, indent=2, sort_keys=True)+"\n")
            r = run(["python3","-m","orda2.orda2_cli","propose","--home",home,"--session",f"sess-{i}"])
            assert r.returncode==0, r.stdout+r.stderr
        r = run(["python3","-m","orda2.orda2_cli","seat","--home",home,"claim","--session","merge-seat"])
        assert r.returncode==0, r.stderr
        for i in range(5):
            r = run(["python3","-m","orda2.orda2_cli","merge","--home",home,"--session","merge-seat","--proposal",f"orda/prop/sess-{i}/1"])
            assert r.returncode==0, f"merge {i} failed: {r.stderr} {r.stdout}"
        r = run(["python3","-m","orda2.orda2_cli","verify","--home",home])
        assert r.returncode==0, r.stdout
        info=json.loads(r.stdout)
        assert info["ok"] is True
        # each slug got bumped
        state=json.load(open(os.path.join(home,"state.json")))
        for slug in slugs:
            assert state["projects"][slug]["summary"].startswith("concurrent edit"), state["projects"][slug]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

