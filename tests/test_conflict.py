
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

def test_conflict():
    """T2: concurrent same-record proposals -> second merge exits 3, conflict in brief."""
    tmp=tempfile.mkdtemp()
    try:
        home=make_home(tmp, V1_SNAPSHOT)
        for sess in ["sess-a","sess-b"]:
            r=run(["python3","-m","orda2.orda2_cli","worktree","--home",home,"new","--session",sess]); assert r.returncode==0
        # sess-a edits proj-x
        wta=os.path.join(home,"worktrees","sess-a","1")
        rec=json.load(open(os.path.join(wta,"records","proj-x.json"))); rec["summary"]="sess-a wins"; open(os.path.join(wta,"records","proj-x.json"),"w").write(json.dumps(rec,indent=2,sort_keys=True)+"\n")
        wtb=os.path.join(home,"worktrees","sess-b","1")
        rec=json.load(open(os.path.join(wtb,"records","proj-x.json"))); rec["summary"]="sess-b conflict"; open(os.path.join(wtb,"records","proj-x.json"),"w").write(json.dumps(rec,indent=2,sort_keys=True)+"\n")
        r=run(["python3","-m","orda2.orda2_cli","propose","--home",home,"--session","sess-a"]); assert r.returncode==0
        r=run(["python3","-m","orda2.orda2_cli","propose","--home",home,"--session","sess-b"]); assert r.returncode==0
        r=run(["python3","-m","orda2.orda2_cli","seat","--home",home,"claim","--session","merge-seat"]); assert r.returncode==0
        r=run(["python3","-m","orda2.orda2_cli","merge","--home",home,"--session","merge-seat","--proposal","orda/prop/sess-a/1"]); assert r.returncode==0
        r=run(["python3","-m","orda2.orda2_cli","merge","--home",home,"--session","merge-seat","--proposal","orda/prop/sess-b/1"])
        assert r.returncode==3, f"expected conflict exit 3 got {r.returncode} {r.stderr} {r.stdout}"
        assert "CONFLICT" in r.stderr and "Never auto-merged" in r.stderr, r.stderr
        assert "git show main:records/proj-x.json" in r.stderr
        assert "git show orda/prop/sess-b/1:records/proj-x.json" in r.stderr
        # no merge commit for conflict — check not merged
        r=run(["python3","-m","orda2.orda2_cli","brief","--home",home])
        assert "CONFLICT" in r.stdout, r.stdout
        assert "Unresolved conflicts" in r.stdout
        # auto-merge never fires: verify file unchanged from sess-a
        rec=json.load(open(os.path.join(home,"records","proj-x.json")))
        assert rec["summary"]=="sess-a wins"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

