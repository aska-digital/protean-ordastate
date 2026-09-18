
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
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

