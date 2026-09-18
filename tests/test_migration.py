
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

def test_migration():
    """T4: from v1 snapshot and 100-record replica -> equality, idempotent, source unchanged."""
    # checksum source before
    import hashlib
    def checksum_dir(d):
        h=hashlib.sha256()
        for root,_,files in os.walk(d):
            for fn in sorted(files):
                h.update(open(os.path.join(root,fn),"rb").read())
        return h.hexdigest()
    src_hash=checksum_dir(V1_SNAPSHOT)
    src_hash_100=checksum_dir(V1_100)
    for snap, expected_records in [(V1_SNAPSHOT,8),(V1_100,100)]:
        tmp=tempfile.mkdtemp()
        try:
            home=make_home(tmp, snap)
            state=json.load(open(os.path.join(home,"state.json")))
            assert len(state["projects"])==expected_records, f"{snap} got {len(state['projects'])}"
            # field-by-field equality vs v1 state
            v1state=json.load(open(os.path.join(snap,"state.json")))
            for slug, rec in v1state["projects"].items():
                nr=state["projects"][slug]
                assert nr["status"]==rec["status"], slug
                # summary may have been preserved
                assert nr["summary"]==rec["summary"], slug
            # idempotent re-run
            r=run(["python3","-m","orda2.orda2_cli","init","--home",home,"--from-v1",snap])
            assert r.returncode==0
            state2=json.load(open(os.path.join(home,"state.json")))
            assert len(state2["projects"])==expected_records
            # source unchanged
            assert checksum_dir(snap)== (src_hash if snap==V1_SNAPSHOT else src_hash_100)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    assert checksum_dir(V1_SNAPSHOT)==src_hash
    assert checksum_dir(V1_100)==src_hash_100

