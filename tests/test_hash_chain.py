"""Regression tests for Shaka QA R2 hash-chain integrity (remediation round 2).

Covers:
  - tampered hash in a middle event → verify 5 + reconcile 5
  - tampered prev_hash → verify 5
  - deleted middle line (seq gap) → verify 5
  - intact chain → verify 0
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

def test_tampered_hash_middle_verify_and_reconcile_exit5():
    tmp = tempfile.mkdtemp()
    try:
        home = make_home(tmp, V1_SNAPSHOT)
        p = os.path.join(home, "events.jsonl")
        lines = open(p).read().splitlines()
        # middle event: index 10 (seq 11)
        idx = 10
        obj = json.loads(lines[idx])
        orig_seq = obj.get("seq")
        obj["hash"] = "deadbeef" + obj["hash"][8:]
        lines[idx] = json.dumps(obj, sort_keys=True)
        open(p, "w").write("\n".join(lines) + "\n")
        r = run(["python3", "-m", "orda2.orda2_cli", "verify", "--home", home])
        assert r.returncode == 5, f"verify should exit 5 on tampered hash, got {r.returncode} {r.stdout} {r.stderr}"
        assert "Traceback" not in r.stderr
        low = (r.stdout + r.stderr).lower()
        assert "hash mismatch" in low or "chain broken" in low
        # also check seq/line named
        assert str(orig_seq) in (r.stdout + r.stderr)
        r2 = run(["python3", "-m", "orda2.orda2_cli", "reconcile", "--home", home])
        assert r2.returncode == 5, f"reconcile should exit 5 on tampered hash, got {r2.returncode} {r2.stdout} {r2.stderr}"
        assert "reconcile refused" in (r2.stdout + r2.stderr).lower()
        assert "Traceback" not in r2.stderr
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def test_tampered_prev_hash_verify_exit5():
    tmp = tempfile.mkdtemp()
    try:
        home = make_home(tmp, V1_SNAPSHOT)
        p = os.path.join(home, "events.jsonl")
        lines = open(p).read().splitlines()
        idx = 5
        obj = json.loads(lines[idx])
        obj["prev_hash"] = "0" * 64
        # keep original hash to isolate prev_hash tamper (hash will also mismatch but that's expected)
        lines[idx] = json.dumps(obj, sort_keys=True)
        open(p, "w").write("\n".join(lines) + "\n")
        r = run(["python3", "-m", "orda2.orda2_cli", "verify", "--home", home])
        assert r.returncode == 5, f"verify should exit 5 on tampered prev_hash, got {r.returncode} {r.stdout}"
        assert "Traceback" not in r.stderr
        assert "prev_hash mismatch" in (r.stdout + r.stderr).lower() or "chain broken" in (r.stdout + r.stderr).lower()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def test_deleted_middle_line_seq_gap_verify_exit5():
    tmp = tempfile.mkdtemp()
    try:
        home = make_home(tmp, V1_SNAPSHOT)
        p = os.path.join(home, "events.jsonl")
        lines = open(p).read().splitlines()
        # delete line 10 (seq 11)
        del lines[10]
        open(p, "w").write("\n".join(lines) + "\n")
        r = run(["python3", "-m", "orda2.orda2_cli", "verify", "--home", home])
        assert r.returncode == 5, f"verify should exit 5 on deleted middle line, got {r.returncode} {r.stdout}"
        assert "Traceback" not in r.stderr
        low = (r.stdout + r.stderr).lower()
        # should mention seq mismatch or prev_hash/chain broken
        assert "seq mismatch" in low or "prev_hash mismatch" in low or "chain broken" in low or "hash mismatch" in low
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def test_intact_chain_verify_ok():
    tmp = tempfile.mkdtemp()
    try:
        home = make_home(tmp, V1_SNAPSHOT)
        r = run(["python3", "-m", "orda2.orda2_cli", "verify", "--home", home])
        assert r.returncode == 0, f"verify should exit 0 on intact chain, got {r.returncode} {r.stdout} {r.stderr}"
        out = json.loads(r.stdout)
        assert out.get("ok") is True
        assert out.get("events") == 22
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
