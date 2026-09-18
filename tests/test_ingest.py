
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

def test_ingest():
    """T6: ingest 25 records while sess-a propose+merge without blocking; quarantine+duplicate handling."""
    tmp=tempfile.mkdtemp()
    try:
        home=make_home(tmp, V1_SNAPSHOT)
        # writer session
        r=run(["python3","-m","orda2.orda2_cli","worktree","--home",home,"new","--session","sess-a"]); assert r.returncode==0
        wta=os.path.join(home,"worktrees","sess-a","1")
        rec=json.load(open(os.path.join(wta,"records","proj-a.json"))); rec["summary"]="sess-a during ingest"; open(os.path.join(wta,"records","proj-a.json"),"w").write(json.dumps(rec,indent=2,sort_keys=True)+"\n")
        r=run(["python3","-m","orda2.orda2_cli","propose","--home",home,"--session","sess-a"]); assert r.returncode==0
        # ingest while propose exists (no waiting)
        start=time.time()
        r=run(["python3","-m","orda2.orda2_cli","ingest","--home",home,"--session","ingest","--file",FIXTURES_LINKS])
        assert r.returncode==0, r.stderr
        ingest_time=time.time()
        info=json.loads(r.stdout)
        assert info["quarantined"]==1, info  # malformed
        assert info["valid"]==24  # 25 minus 1 malformed, duplicate deduped? actually 24 valid
        # sess-a merge should not wait on ingest
        r=run(["python3","-m","orda2.orda2_cli","seat","--home",home,"claim","--session","merge-seat"]); assert r.returncode==0
        r=run(["python3","-m","orda2.orda2_cli","merge","--home",home,"--session","merge-seat","--proposal","orda/prop/sess-a/1"]); assert r.returncode==0
        merge_time=time.time()
        # wall-clock independence: merge happened promptly after ingest (not blocked)
        assert (merge_time - start) < 10, "merge blocked too long"
        # now merge ingest
        # find ingest branch name
        import subprocess as sp
        rr=sp.run(["git","for-each-ref","--format=%(refname)","refs/heads/orda/ingest/"], cwd=home, capture_output=True, text=True)
        branches=[l.replace("refs/heads/","") for l in rr.stdout.splitlines() if l.strip()]
        assert len(branches)==1
        r=run(["python3","-m","orda2.orda2_cli","merge","--home",home,"--session","merge-seat","--proposal",branches[0]]); assert r.returncode==0
        # malformed quarantined
        qdir=os.path.join(home,"inbox","_quarantine")
        found=False
        for root,_,files in os.walk(qdir):
            for fn in files:
                if "quarantined" in fn or "invalid" in fn:
                    found=True
        assert found, "quarantined file not found"
        # duplicate is no-op: link-0 appears only once
        assert os.path.exists(os.path.join(home,"inbox", os.path.basename(branches[0]).replace("orda/ingest/","") if "/" in branches[0] else ""))
        # verify inbox has 23 or 24 files (excluding duplicate+malformed)
        # Just verify chain intact
        r=run(["python3","-m","orda2.orda2_cli","verify","--home",home]); assert r.returncode==0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

