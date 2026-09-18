
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

def test_reader_roundtrip():
    """T5: propose->merge->brief shows change; join cost one read."""
    tmp=tempfile.mkdtemp()
    try:
        home=make_home(tmp, V1_SNAPSHOT)
        r=run(["python3","-m","orda2.orda2_cli","worktree","--home",home,"new","--session","writer"]); assert r.returncode==0
        wt=os.path.join(home,"worktrees","writer","1")
        rec=json.load(open(os.path.join(wt,"records","proj-b.json"))); rec["summary"]="reader test change"; open(os.path.join(wt,"records","proj-b.json"),"w").write(json.dumps(rec,indent=2,sort_keys=True)+"\n")
        r=run(["python3","-m","orda2.orda2_cli","propose","--home",home,"--session","writer"]); assert r.returncode==0
        r=run(["python3","-m","orda2.orda2_cli","seat","--home",home,"claim","--session","seat1"]); assert r.returncode==0
        r=run(["python3","-m","orda2.orda2_cli","merge","--home",home,"--session","seat1","--proposal","orda/prop/writer/1"]); assert r.returncode==0
        # reader fresh: one read of brief
        r=run(["python3","-m","orda2.orda2_cli","brief","--home",home,"--format","md"])
        assert r.returncode==0
        assert "reader test change" in r.stdout
        # brief current as of last merge: revision matches state
        brief_json=json.loads(run(["python3","-m","orda2.orda2_cli","brief","--home",home,"--format","json"]).stdout)
        state=json.load(open(os.path.join(home,"state.json")))
        assert brief_json["revision"]==state["revision"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

