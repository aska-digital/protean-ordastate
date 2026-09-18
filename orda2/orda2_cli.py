#!/usr/bin/env python3
"""CLI for Orda State v2 — stdlib + git, exit codes 0/2/3/4/5."""
import argparse
import json
import os
import sys
import subprocess
import hashlib
import datetime
import re
import shutil
import glob

# allow running as python -m orda2.orda2_cli and as script
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orda2.orda2_store import Store, StoreError, EXIT_CONFLICT, EXIT_LEASE, EXIT_INTEGRITY, EXIT_USAGE, Lock, atomic_write, run_git, git_head, utcnow, parse_utc, slug_valid, scan_for_secrets, assert_no_secrets, check_main_clean
from orda2.orda2_events import ZERO_HASH, event_hash, read_events, verify_chain, write_events, rechain_segment
from orda2.orda2_migration import init_home

DEFAULT_HOME = os.path.expanduser("~/.hermes/eldunari/nexus/state/orda")


def require_home(args):
    return os.path.abspath(os.path.expanduser(args.home))


def cmd_init(args):
    home = require_home(args)
    from_v1 = os.path.abspath(os.path.expanduser(args.from_v1)) if args.from_v1 else None
    if from_v1 and not os.path.isdir(from_v1):
        raise StoreError(EXIT_USAGE, "from-v1 not found: %s" % from_v1)
    # idempotency guard
    if os.path.exists(os.path.join(home, "state.json")) and os.path.isdir(os.path.join(home, ".git")):
        # check if already imported same v1
        print(json.dumps({"home": home, "already": True, "note": "already initialized"}))
        return 0
    result = init_home(home, from_v1=from_v1)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_worktree_new(args):
    home = require_home(args)
    session = args.session
    if not session or not re.match(r"^[A-Za-z0-9._-]+$", session):
        raise StoreError(EXIT_USAGE, "invalid --session (alnum, ., _, -)")
    store = Store(home)
    if not store.exists():
        raise StoreError(EXIT_USAGE, "not initialized (run init --home %s)" % home)
    # find next n for this session
    existing = []
    try:
        r = run_git(["for-each-ref", "--format=%(refname)", "refs/heads/orda/prop/%s/" % session], cwd=home)
        for line in r.stdout.splitlines():
            m = re.search(r"/(\d+)$", line.strip())
            if m:
                existing.append(int(m.group(1)))
    except StoreError:
        pass
    n = (max(existing) + 1) if existing else 1
    branch = "orda/prop/%s/%d" % (session, n)
    # worktree path
    wt_base = os.path.join(home, "worktrees", session)
    wt_path = os.path.join(wt_base, str(n))
    os.makedirs(os.path.dirname(wt_path), exist_ok=True)
    # create worktree with new branch off main
    # ensure main is checked out at home
    run_git(["worktree", "add", "-b", branch, wt_path, "main"], cwd=home)
    # also set git config in worktree
    run_git(["config", "user.email", "orda2@local"], cwd=wt_path)
    run_git(["config", "user.name", "orda2"], cwd=wt_path)
    print(json.dumps({"worktree": wt_path, "branch": branch, "session": session, "n": n}, indent=2, sort_keys=True))
    return 0


def _get_worktree_path(home, session, n=None):
    """Find worktree path for session (latest if n not given)."""
    base = os.path.join(home, "worktrees", session)
    if n is not None:
        return os.path.join(base, str(n))
    # find max n
    if not os.path.isdir(base):
        return None
    candidates = []
    for entry in os.listdir(base):
        if entry.isdigit():
            candidates.append(int(entry))
    if not candidates:
        return None
    return os.path.join(base, str(max(candidates)))


def _branch_for_session(home, session):
    wt = _get_worktree_path(home, session)
    if not wt or not os.path.isdir(wt):
        raise StoreError(EXIT_USAGE, "no worktree for session %r (run worktree new --session %s)" % (session, session))
    r = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=wt)
    branch = r.stdout.strip()
    return wt, branch


def cmd_propose(args):
    home = require_home(args)
    session = args.session
    # --- main cleanliness gate (attack 8): refuse if main is dirty ---
    if os.path.isdir(os.path.join(home, ".git")):
        check_main_clean(home)
    wt, branch = _branch_for_session(home, session)
    store = Store(home)

    # optionally apply a batch file: each entry is a project-like dict with slug
    batch_records = []
    if args.file:
        batch_path = os.path.abspath(os.path.expanduser(args.file))
        if not os.path.exists(batch_path):
            raise StoreError(EXIT_USAGE, "batch file not found: %s" % batch_path)
        data = json.load(open(batch_path))
        # accept list or {records: [...]}
        if isinstance(data, dict) and "records" in data:
            batch_records = data["records"]
        elif isinstance(data, list):
            batch_records = data
        else:
            batch_records = [data]
        for rec in batch_records:
            slug = rec.get("slug")
            if not slug or not slug_valid(slug):
                raise StoreError(EXIT_USAGE, "invalid slug in batch file: %r" % slug)
            # write records/<slug>.json in worktree
            rec_path = os.path.join(wt, "records", "%s.json" % slug)
            # load current rev if exists
            existing = {}
            if os.path.exists(rec_path):
                try:
                    existing = json.load(open(rec_path))
                except Exception:
                    existing = {}
            # compute rev increment later at merge; for now set writer etc
            enriched = dict(existing)
            enriched.update(rec)
            enriched["slug"] = slug
            enriched["writer"] = session
            enriched["updated_utc"] = utcnow()
            # --- secrets scan (attack 7): refuse token-shaped values, redact value ---
            _scan_text = json.dumps(enriched, sort_keys=True)
            _hit = scan_for_secrets(_scan_text, "records/%s.json" % slug)
            if _hit:
                raise StoreError(EXIT_USAGE, "secrets scan hit in records/%s.json: pattern %s — value redacted (refused)" % (slug, _hit))
            # keep rev for now as existing rev (CAS check at merge)
            os.makedirs(os.path.dirname(rec_path), exist_ok=True)
            atomic_write(rec_path, json.dumps(enriched, indent=2, sort_keys=True) + "\n")

    # Also scan any existing modified records in worktree for secrets (covers manual edits without --file)
    try:
        _r = run_git(["status", "--porcelain", "--", "records"], cwd=wt, check=False)
        for _line in _r.stdout.splitlines():
            _parts = _line.strip().split()
            if not _parts:
                continue
            _fp = _parts[-1]
            if _fp.startswith("records/") and _fp.endswith(".json"):
                _full = os.path.join(wt, _fp)
                if os.path.exists(_full):
                    _t = open(_full).read()
                    _hit = scan_for_secrets(_t, _fp)
                    if _hit:
                        raise StoreError(EXIT_USAGE, "secrets scan hit in %s: pattern %s — value redacted (refused)" % (_fp, _hit))
    except StoreError:
        raise
    except Exception:
        pass

    if args.rebase:
        # Manual rebase: save changed records, reset to main, reapply
        # Collect changed slugs from current HEAD vs main merge-base
        try:
            rb = run_git(["merge-base", "main", branch], cwd=wt, check=False)
            rb_commit = rb.stdout.strip() if rb.returncode==0 else git_head(home)
            rdiff = run_git(["diff", "--name-only", rb_commit, "HEAD"], cwd=wt, check=False)
            rebase_slugs = set()
            for line in rdiff.stdout.splitlines():
                line=line.strip()
                if line.startswith("records/") and line.endswith(".json"):
                    rebase_slugs.add(line[len("records/"):-len(".json")])
            # also include status
            rs = run_git(["status", "--porcelain"], cwd=wt, check=False)
            for line in rs.stdout.splitlines():
                parts=line.strip().split()
                if parts:
                    fp=parts[-1]
                    if fp.startswith("records/") and fp.endswith(".json"):
                        rebase_slugs.add(fp[len("records/"):-len(".json")])
            # save content of those records
            saved = {}
            for slug in rebase_slugs:
                p=os.path.join(wt,"records","%s.json" % slug)
                if os.path.exists(p):
                    saved[slug]=open(p).read()
            # hard reset to main
            run_git(["reset", "--hard", "main"], cwd=wt)
            # reapply saved records (merged resolution: keep our summary, update rev base)
            for slug, content in saved.items():
                try:
                    rec=json.loads(content)
                    # ensure we keep our edits (summary etc) but will be rebased
                    dest=os.path.join(wt,"records","%s.json" % slug)
                    atomic_write(dest, content if content.endswith("\n") else content+"\n")
                except Exception:
                    pass
        except Exception as e:
            # fallback: try git rebase
            r = run_git(["rebase", "main"], cwd=wt, check=False)
            if r.returncode != 0:
                run_git(["rebase", "--abort"], cwd=wt, check=False)
                raise StoreError(EXIT_CONFLICT, "rebase conflict — resolve manually: %s" % e)
        # continue to manifest regeneration

    # compute base_main_commit = merge-base or main HEAD at branch creation? Use main HEAD
    # For correctness, base_main_commit should be the commit the branch was based on
    # We approximate as merge-base(branch, main)
    try:
        r = run_git(["merge-base", "main", branch], cwd=wt)
        base_commit = r.stdout.strip()
    except StoreError:
        base_commit = git_head(home)

    # compute base_revs: read records at base_commit
    base_revs = {}
    # list slugs changed in this branch vs base
    r = run_git(["diff", "--name-only", base_commit, branch], cwd=wt, check=False)
    changed_slugs = set()
    for line in r.stdout.splitlines():
        line=line.strip()
        if line.startswith("records/") and line.endswith(".json"):
            slug = line[len("records/"):-len(".json")]
            changed_slugs.add(slug)
    # also include uncommitted changes in worktree (HEAD vs working tree)
    r2 = run_git(["status", "--porcelain"], cwd=wt, check=False)
    for line in r2.stdout.splitlines():
        # format: " M records/proj-a.json" or "?? records/new.json"
        parts = line.strip().split()
        if not parts:
            continue
        fp = parts[-1]
        if fp.startswith("records/") and fp.endswith(".json"):
            slug = fp[len("records/"):-len(".json")]
            changed_slugs.add(slug)
    # also include any records we just wrote
    for rec in batch_records:
        changed_slugs.add(rec.get("slug"))

    # For each changed slug, read base rev via git show
    for slug in changed_slugs:
        text = None
        try:
            rr = run_git(["show", "%s:records/%s.json" % (base_commit, slug)], cwd=home, check=False)
            if rr.returncode == 0:
                text = rr.stdout
        except Exception:
            pass
        if text:
            try:
                rec = json.loads(text)
                base_revs[slug] = int(rec.get("rev", 0))
            except Exception:
                base_revs[slug] = 0
        else:
            base_revs[slug] = 0

    # Build event segment: one event per changed record (kind update)
    main_events = read_events(os.path.join(home, "events.jsonl"))
    head_hash = main_events[-1]["hash"] if main_events else ZERO_HASH
    head_seq = (main_events[-1]["seq"] + 1) if main_events else 1
    # Also need base head for chain chain: for first propose, prev_hash = head_hash
    # But after rebases, head may have moved; we always chain from current main head

    segment_events = []
    prev = head_hash
    seq = head_seq
    now = utcnow()
    for slug in sorted(changed_slugs):
        # read current record in worktree
        rec_path = os.path.join(wt, "records", "%s.json" % slug)
        if not os.path.exists(rec_path):
            continue
        rec = json.load(open(rec_path))
        ev = {
            "seq": seq,
            "prev_hash": prev,
            "revision": 0,  # will be renumbered at merge
            "ts": now,
            "writer": session,
            "kind": "update",
            "project": slug,
            "data": {k: rec.get(k) for k in ("status", "summary", "next", "detail_ref", "tags", "blocker", "writer") if rec.get(k) is not None},
            "note": "propose %s" % branch,
        }
        # compute hash
        tmp = dict(ev)
        tmp.pop("hash", None)
        ev["hash"] = event_hash(prev, tmp)
        segment_events.append(ev)
        prev = ev["hash"]
        seq += 1

    if not segment_events and not args.rebase:
        raise StoreError(EXIT_USAGE, "no changes to propose (edit records/ first or pass --file)")

    # Write segment file in worktree
    seg_path = os.path.join(wt, "events.proposal.jsonl")
    with open(seg_path, "w") as f:
        for e in segment_events:
            f.write(json.dumps(e, sort_keys=True) + "\n")

    # Manifest
    manifest = {
        "id": branch.replace("/", "-"),
        "session": session,
        "branch": branch,
        "kind": "work",
        "base_main_commit": base_commit,
        "base_revs": base_revs,
        "event_head_hash": segment_events[-1]["hash"] if segment_events else head_hash,
        "events": "events.proposal.jsonl",
        "event_count": len(segment_events),
        "note": args.note or "",
        "created_utc": now,
    }
    atomic_write(os.path.join(wt, "proposal.json"), json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    # commit in worktree
    run_git(["add", "records", "proposal.json", "events.proposal.jsonl"], cwd=wt, check=False)
    # also add new files
    status = run_git(["status", "--porcelain"], cwd=wt)
    if not status.stdout.strip():
        print(json.dumps({"branch": branch, "note": "nothing to commit"}, indent=2, sort_keys=True))
        return 0
    run_git(["commit", "-m", "propose %s: %d events base %s" % (branch, len(segment_events), base_commit[:8])], cwd=wt)

    print(json.dumps({"branch": branch, "worktree": wt, "events": len(segment_events), "base_revs": base_revs, "manifest": manifest}, indent=2, sort_keys=True))
    return 0


def cmd_ingest(args):
    home = require_home(args)
    session = args.session
    batch_file = os.path.abspath(os.path.expanduser(args.file))
    if not os.path.exists(batch_file):
        raise StoreError(EXIT_USAGE, "ingest file not found: %s" % batch_file)
    data = json.load(open(batch_file))
    items = data if isinstance(data, list) else data.get("records") or data.get("items") or [data]
    batch_id = args.batch or datetime.datetime.now(datetime.timezone.utc).strftime("batch-%Y%m%d-%H%M%S")
    # create ingest branch worktree
    store = Store(home)
    # main cleanliness gate (attack 8)
    if os.path.isdir(os.path.join(home, ".git")):
        check_main_clean(home)
    branch = "orda/ingest/%s" % batch_id
    # if branch already exists, use existing worktree
    wt_path = os.path.join(home, "worktrees", "_ingest", batch_id)
    if not os.path.exists(os.path.join(home, ".git")):
        raise StoreError(EXIT_USAGE, "not initialized")
    # check if branch exists
    exists = run_git(["show-ref", "--verify", "refs/heads/%s" % branch], cwd=home, check=False).returncode == 0
    if not exists:
        os.makedirs(os.path.dirname(wt_path), exist_ok=True)
        run_git(["worktree", "add", "-b", branch, wt_path, "main"], cwd=home)
        run_git(["config", "user.email", "orda2@local"], cwd=wt_path)
        run_git(["config", "user.name", "orda2"], cwd=wt_path)
    else:
        # ensure worktree exists
        if not os.path.isdir(wt_path):
            # re-add worktree
            os.makedirs(os.path.dirname(wt_path), exist_ok=True)
            run_git(["worktree", "add", wt_path, branch], cwd=home)
    # need inbox dir
    # dedupe + quarantine logic
    quarantine = []
    inbox_dir = os.path.join(wt_path, "inbox", batch_id)
    os.makedirs(inbox_dir, exist_ok=True)
    # load existing slugs to dedupe (check main records + inbox)
    state = store.load_state() if os.path.exists(store.state_path) else {"projects": {}}
    existing_slugs = set(state.get("projects", {}).keys())
    # also check inbox already present
    valid_count = 0
    seen_hashes = set()
    for idx, item in enumerate(items):
        slug = item.get("slug")
        content_hash = hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()[:12]
        # validation
        if not slug or not slug_valid(slug):
            quarantine.append({"item": item, "reason": "invalid slug"})
            continue
        # secrets scan — broadened (attack 7): refuse token-shaped values
        text = json.dumps(item)
        _hit = scan_for_secrets(text, "inbox/%s/%s.json" % (batch_id, slug))
        if _hit:
            raise StoreError(EXIT_USAGE, "secrets scan hit in inbox/%s/%s.json: pattern %s — value redacted (refused)" % (batch_id, slug, _hit))
            continue
        # dedupe
        if slug in existing_slugs or content_hash in seen_hashes:
            # duplicate is no-op, skip
            continue
        seen_hashes.add(content_hash)
        exists_in_records = os.path.exists(os.path.join(home, "records", "%s.json" % slug))
        if exists_in_records:
            # candidate file
            out_path = os.path.join(inbox_dir, "%s.candidate.json" % slug)
        else:
            out_path = os.path.join(inbox_dir, "%s.json" % slug)
        enriched = dict(item)
        enriched.setdefault("status", "active")
        enriched["ingested_utc"] = utcnow()
        enriched["writer"] = session
        atomic_write(out_path, json.dumps(enriched, indent=2, sort_keys=True) + "\n")
        valid_count += 1
        # commit in batches of 20 (spec says every 20 or 60s — we batch 20)
        if valid_count % 20 == 0:
            run_git(["add", "inbox"], cwd=wt_path, check=False)
            st = run_git(["status", "--porcelain"], cwd=wt_path, check=False)
            if st.stdout.strip():
                run_git(["commit", "-m", "ingest %s: %d items" % (batch_id, valid_count)], cwd=wt_path)

    # quarantine files
    if quarantine:
        qdir = os.path.join(wt_path, "inbox", "_quarantine", batch_id)
        os.makedirs(qdir, exist_ok=True)
        for q in quarantine:
            fname = "%s.quarantined.json" % (q["item"].get("slug") or "invalid-%d" % len(os.listdir(qdir)))
            atomic_write(os.path.join(qdir, fname), json.dumps(q, indent=2, sort_keys=True) + "\n")

    # build segment + manifest
    main_events = read_events(os.path.join(home, "events.jsonl"))
    head_hash = main_events[-1]["hash"] if main_events else ZERO_HASH
    seq = (main_events[-1]["seq"] + 1) if main_events else 1
    now = utcnow()
    base_commit = git_head(home)
    # segment: one ingest event per valid item + one quarantine note if any?
    segment = []
    prev = head_hash
    for i in range(valid_count):
        ev = {"seq": seq, "prev_hash": prev, "revision": 0, "ts": now, "writer": session, "kind": "ingest", "project": None, "data": {"batch": batch_id, "index": i}, "note": "ingest %s" % batch_id}
        tmp = dict(ev); tmp.pop("hash", None)
        ev["hash"] = event_hash(prev, tmp)
        segment.append(ev)
        prev = ev["hash"]
        seq += 1
    if quarantine:
        ev = {"seq": seq, "prev_hash": prev, "revision": 0, "ts": now, "writer": session, "kind": "ingest", "project": None, "data": {"batch": batch_id, "quarantined": len(quarantine)}, "note": "quarantined %d" % len(quarantine)}
        tmp = dict(ev); tmp.pop("hash", None)
        ev["hash"] = event_hash(prev, tmp)
        segment.append(ev)

    seg_path = os.path.join(wt_path, "events.proposal.jsonl")
    with open(seg_path, "w") as f:
        for e in segment:
            f.write(json.dumps(e, sort_keys=True) + "\n")

    manifest = {
        "id": branch.replace("/", "-"),
        "session": session,
        "branch": branch,
        "kind": "ingest",
        "base_main_commit": base_commit,
        "base_revs": {},
        "event_head_hash": segment[-1]["hash"] if segment else head_hash,
        "events": "events.proposal.jsonl",
        "event_count": len(segment),
        "batch": batch_id,
        "valid": valid_count,
        "quarantined": len(quarantine),
        "created_utc": now,
    }
    atomic_write(os.path.join(wt_path, "proposal.json"), json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    run_git(["add", "inbox", "proposal.json", "events.proposal.jsonl"], cwd=wt_path, check=False)
    # also quarantine
    if quarantine:
        run_git(["add", "inbox/_quarantine"], cwd=wt_path, check=False)
    st = run_git(["status", "--porcelain"], cwd=wt_path, check=False)
    if st.stdout.strip():
        run_git(["commit", "-m", "ingest propose %s: %d valid, %d quarantined" % (batch_id, valid_count, len(quarantine))], cwd=wt_path)

    print(json.dumps({"branch": branch, "worktree": wt_path, "valid": valid_count, "quarantined": len(quarantine), "manifest": manifest}, indent=2, sort_keys=True))
    return 0


def cmd_seat(args):
    home = require_home(args)
    store = Store(home)
    if args.action == "claim":
        seat = store.claim(args.session, ttl_s=args.ttl, steal=args.steal)
        print(json.dumps(seat, indent=2, sort_keys=True))
    elif args.action == "renew":
        seat = store.renew(args.session, ttl_s=args.ttl)
        print(json.dumps(seat, indent=2, sort_keys=True))
    elif args.action == "release":
        seat = store.release(args.session)
        print(json.dumps(seat, indent=2, sort_keys=True))
    else:
        raise StoreError(EXIT_USAGE, "seat action must be claim|renew|release")
    return 0


def cmd_review(args):
    home = require_home(args)
    store = Store(home)
    proposals = store.list_proposals()
    ingests = store.list_ingest_proposals()
    all_props = proposals + ingests
    # simple wait loop if requested
    if args.wait:
        import time
        timeout = int(args.timeout or 30)
        start = datetime.datetime.now(datetime.timezone.utc)
        while not all_props and (datetime.datetime.now(datetime.timezone.utc) - start).total_seconds() < timeout:
            time.sleep(1)
            all_props = store.list_proposals() + store.list_ingest_proposals()
        if not all_props:
            print(json.dumps({"queue": [], "note": "timeout"}, indent=2, sort_keys=True))
            return EXIT_LEASE  # per spec exit 4 on timeout? but we return lease code
    # render
    out = {"proposals": proposals, "ingests": ingests, "total": len(all_props)}
    # human-readable
    if not args.json:
        for p in proposals:
            mani = p.get("manifest") or {}
            print("%s kind=%s session=%s events=%s base=%s" % (p["branch"], mani.get("kind"), mani.get("session"), mani.get("event_count"), (mani.get("base_main_commit") or "")[:8]))
        for p in ingests:
            mani = p.get("manifest") or {}
            print("%s kind=ingest valid=%s quarantined=%s" % (p["branch"], mani.get("valid"), mani.get("quarantined")))
        if not all_props:
            print("(empty queue)")
    else:
        print(json.dumps(out, indent=2, sort_keys=True))
    return 0


def cmd_merge(args):
    home = require_home(args)
    session = args.session
    proposal_branch = args.proposal
    store = Store(home)
    # require seat
    store.require_seat(session)

    # --- main cleanliness gate (attack 8): merge refuses when main has foreign uncommitted changes ---
    check_main_clean(home)

    # load manifest from proposal branch
    manifest_text = None
    try:
        r = run_git(["show", "%s:proposal.json" % proposal_branch], cwd=home)
        manifest_text = r.stdout
    except StoreError:
        raise StoreError(EXIT_USAGE, "proposal not found: %s" % proposal_branch)
    manifest = json.loads(manifest_text)
    kind = manifest.get("kind", "work")
    base_revs = manifest.get("base_revs", {})

    # ---- GATE G1-G5 ----
    # G1 schema
    if not manifest.get("session") or not manifest.get("base_main_commit"):
        raise StoreError(EXIT_USAGE, "invalid manifest G1")

    # G3 precondition: per-record rev check
    conflicts = []
    for slug, base_rev in base_revs.items():
        # read current main rev for slug
        current_rev = 0
        rec_path = os.path.join(home, "records", "%s.json" % slug)
        if os.path.exists(rec_path):
            try:
                rec = json.load(open(rec_path))
                current_rev = int(rec.get("rev", 0))
            except Exception:
                current_rev = 0
        else:
            current_rev = 0
        if current_rev != int(base_rev):
            # need writer info for message
            current_writer = "?"
            current_ts = ""
            if os.path.exists(rec_path):
                try:
                    rec = json.load(open(rec_path))
                    current_writer = rec.get("writer") or rec.get("last_writer") or "?"
                    current_ts = rec.get("updated_utc") or ""
                except Exception:
                    pass
            conflicts.append({"slug": slug, "base_rev": base_rev, "main_rev": current_rev, "main_writer": current_writer, "main_ts": current_ts})

    if conflicts:
        # write proposal-conflicts/<proposal-id>.json
        c = conflicts[0]
        cid = proposal_branch.replace("/", "-")
        conflict_record = {
            "proposal": proposal_branch,
            "id": cid,
            "slug": c["slug"],
            "base_rev": c["base_rev"],
            "main_rev": c["main_rev"],
            "proposal_writer": manifest.get("session"),
            "proposal_ts": manifest.get("created_utc"),
            "main_writer": c["main_writer"],
            "main_ts": c["main_ts"],
            "both_record_paths": ["main:records/%s.json" % c["slug"], "%s:records/%s.json" % (proposal_branch, c["slug"])],
            "created_utc": utcnow(),
            "branch": proposal_branch,
        }
        os.makedirs(os.path.join(home, "proposal-conflicts"), exist_ok=True)
        atomic_write(os.path.join(home, "proposal-conflicts", "%s.json" % cid), json.dumps(conflict_record, indent=2, sort_keys=True) + "\n")
        # append conflict event to main chain
        with Lock(home):
            events = read_events(store.events_path)
            prev = events[-1]["hash"] if events else ZERO_HASH
            seq = (events[-1]["seq"] + 1) if events else 1
            last_rev = events[-1]["revision"] if events else 0
            ev = {"seq": seq, "prev_hash": prev, "revision": last_rev + 1, "ts": utcnow(), "writer": session, "kind": "conflict", "project": c["slug"], "data": conflict_record, "note": "conflict refused %s" % proposal_branch}
            tmp = dict(ev); tmp.pop("hash", None)
            ev["hash"] = event_hash(prev, tmp)
            with open(store.events_path, "a") as f:
                f.write(json.dumps(ev, sort_keys=True) + "\n")
                f.flush(); os.fsync(f.fileno())
            # update state revision to last_rev+1 so verify stays consistent
            if os.path.exists(store.state_path):
                try:
                    state = json.load(open(store.state_path))
                    state["revision"] = ev["revision"]
                    state["updated_utc"] = ev["ts"]
                    atomic_write(store.state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")
                except Exception:
                    pass
            # regenerate projection to surface conflict
            try:
                from orda2.orda2_projection import write_projection
                write_projection(home)
            except Exception:
                pass
            # commit conflict record + events + state
            try:
                run_git(["add", "proposal-conflicts", "events.jsonl", "state.json", "projection"], cwd=home, check=False)
                st = run_git(["status", "--porcelain"], cwd=home, check=False)
                if st.stdout.strip():
                    run_git(["commit", "-m", "conflict: %s vs main record %s" % (proposal_branch, c["slug"])], cwd=home)
            except Exception:
                pass
            # verdict file
            os.makedirs(os.path.join(home, "reviews"), exist_ok=True)
            atomic_write(os.path.join(home, "reviews", "%s.json" % cid), json.dumps({"proposal": proposal_branch, "verdict": "conflict", "conflict": conflict_record, "gate": "G3"}, indent=2, sort_keys=True) + "\n")
            try:
                run_git(["add", "reviews"], cwd=home, check=False)
                st = run_git(["status", "--porcelain", "reviews/%s.json" % cid], cwd=home, check=False)
                if st.stdout.strip():
                    run_git(["commit", "-m", "verdict conflict %s" % cid], cwd=home)
            except Exception:
                pass

        # human-visible exact message per spec §4/§11
        msg = ("CONFLICT %s vs main — record: %s\n"
               "  proposal base rev: %s (writer %s, %s)\n"
               "  main rev:          %s (writer %s, %s)\n"
               "  view both:  git show main:records/%s.json\n"
               "              git show %s:records/%s.json\n"
               "  resolve:    repropose on the newer rev, or decide as reviewer\n"
               "Refused: same-record concurrent change. Never auto-merged. See brief → Unresolved conflicts. Resolve: orda2 propose --rebase." % (
                   proposal_branch, c["slug"],
                   c["base_rev"], manifest.get("session"), manifest.get("created_utc") or "",
                   c["main_rev"], c["main_writer"], c["main_ts"] or "",
                   c["slug"], proposal_branch, c["slug"]))
        print(msg, file=sys.stderr)
        return EXIT_CONFLICT

    # G4 secrets scan (attack 7): pre-merge gate — refuse if proposal would land a secret
    try:
        # Scan proposal branch records/inbox files for secrets
        _diff_r = run_git(["diff", "--name-only", manifest.get("base_main_commit", "main"), proposal_branch], cwd=home, check=False)
        for _fp in [l.strip() for l in _diff_r.stdout.splitlines() if l.strip()]:
            if _fp.startswith("records/") or _fp.startswith("inbox/"):
                _c = run_git(["show", "%s:%s" % (proposal_branch, _fp)], cwd=home, check=False)
                if _c.returncode == 0:
                    _hit = scan_for_secrets(_c.stdout, _fp)
                    if _hit:
                        raise StoreError(EXIT_USAGE, "secrets scan hit in %s (proposal %s): pattern %s — value redacted (merge refused)" % (_fp, proposal_branch, _hit))
    except StoreError:
        raise
    except Exception:
        pass

    # G2 chain check: segment prev == main head (or needs repropose)
    # For ingestion kind, G3 always passes (namespace disjoint) so we skip conflict above — but we already handled.
    # Now perform merge
    # Load proposal segment events
    seg_text = run_git(["show", "%s:events.proposal.jsonl" % proposal_branch], cwd=home, check=False)
    segment = []
    if seg_text.returncode == 0 and seg_text.stdout.strip():
        for line in seg_text.stdout.splitlines():
            if line.strip():
                segment.append(json.loads(line))

    # crash injection hooks
    crash_after = os.environ.get("ORDA2_CRASH_AFTER", "")

    with Lock(home):
        main_events = read_events(store.events_path)
        main_head_hash = main_events[-1]["hash"] if main_events else ZERO_HASH
        main_head_seq = (main_events[-1]["seq"] + 1) if main_events else 1
        main_last_rev = main_events[-1]["revision"] if main_events else 0

        # rechain segment onto current main head
        re_chained = rechain_segment(segment, main_head_hash, main_head_seq)
        # renumber revision sequentially (per-event revision = last_rev + idx +1)
        for idx, e in enumerate(re_chained):
            e["revision"] = main_last_rev + idx + 1
            # recompute hash because revision changed
            prev_h = re_chained[idx-1]["hash"] if idx > 0 else main_head_hash
            e["prev_hash"] = prev_h
            tmp = dict(e); tmp.pop("hash", None)
            e["hash"] = event_hash(prev_h, tmp)

        # Stage 1: splice events into main events.jsonl
        with open(store.events_path, "a") as f:
            for e in re_chained:
                f.write(json.dumps(e, sort_keys=True) + "\n")
            f.flush(); os.fsync(f.fileno())

        if crash_after == "splice":
            print("CRASH injected after splice", file=sys.stderr)
            os._exit(1)

        # Stage 2: move records / inbox from proposal branch into main worktree
        # Use git merge --no-commit approach or cherry-pick files via git show
        # Simplest: git checkout branch -- records inbox etc. then commit
        # For work kind, copy records; for ingest, copy inbox + quarantine
        try:
            # get list of files changed in proposal vs its base
            base_commit = manifest.get("base_main_commit")
            # checkout the proposal's records/inbox into main
            # Use git checkout <branch> -- records inbox
            # But to avoid overwriting unrelated, we checkout specific paths
            # First, get diff files
            r = run_git(["diff", "--name-only", base_commit, proposal_branch], cwd=home, check=False)
            files_to_checkout = [line.strip() for line in r.stdout.splitlines() if line.strip() and (line.strip().startswith("records/") or line.strip().startswith("inbox/"))]
            for fp in files_to_checkout:
                # git show branch:fp > home/fp
                rr = run_git(["show", "%s:%s" % (proposal_branch, fp)], cwd=home, check=False)
                if rr.returncode == 0:
                    dest = os.path.join(home, fp)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    # For records, bump rev
                    if fp.startswith("records/"):
                        try:
                            rec = json.loads(rr.stdout)
                            # bump rev to new value (main_last_rev relative)
                            # Find which event touches this slug
                            slug = fp[len("records/"):-len(".json")]
                            # latest event for this slug in re_chained
                            # rev should be revision of that event
                            rev = None
                            for ev in reversed(re_chained):
                                if ev.get("project") == slug:
                                    rev = ev["revision"]
                                    break
                            if rev is not None:
                                rec["rev"] = rev
                                rec["head_event"] = next((ev["hash"] for ev in reversed(re_chained) if ev.get("project")==slug), rec.get("head_event"))
                            atomic_write(dest, json.dumps(rec, indent=2, sort_keys=True) + "\n")
                        except Exception:
                            atomic_write(dest, rr.stdout if rr.stdout.endswith("\n") else rr.stdout + "\n")
                    else:
                        atomic_write(dest, rr.stdout if rr.stdout.endswith("\n") else rr.stdout + "\n")
            # Also need to update state.json projects from new records
            state = json.load(open(store.state_path))
            for fp in files_to_checkout:
                if fp.startswith("records/"):
                    slug = fp[len("records/"):-len(".json")]
                    rec = json.load(open(os.path.join(home, fp)))
                    state["projects"][slug] = rec
            # Also for inbox kind, inbox already copied
        except Exception as e:
            print("records-move failed: %s" % e, file=sys.stderr)
            raise

        if crash_after == "records-move":
            print("CRASH injected after records-move", file=sys.stderr)
            os._exit(1)

        # Append merge event
        merge_rev = (re_chained[-1]["revision"] + 1) if re_chained else (main_last_rev + 1)
        # if no events in segment (should not happen) handle
        merge_prev = re_chained[-1]["hash"] if re_chained else main_head_hash
        merge_seq = (re_chained[-1]["seq"] + 1) if re_chained else main_head_seq
        merge_ev = {"seq": merge_seq, "prev_hash": merge_prev, "revision": merge_rev, "ts": utcnow(), "writer": session, "kind": "merge", "project": None, "data": {"proposal": proposal_branch, "session": manifest.get("session"), "base": base_commit, "merged_events": len(re_chained), "verdict": "accept"}, "note": "merge %s" % proposal_branch}
        tmp = dict(merge_ev); tmp.pop("hash", None)
        merge_ev["hash"] = event_hash(merge_prev, tmp)
        with open(store.events_path, "a") as f:
            f.write(json.dumps(merge_ev, sort_keys=True) + "\n")
            f.flush(); os.fsync(f.fileno())

        # Update state revision to merge_rev
        state["revision"] = merge_rev
        state["updated_utc"] = merge_ev["ts"]
        state["writer"] = session
        atomic_write(store.state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")

        if crash_after == "projection":
            # crash before projection write — leave stale projection
            print("CRASH injected before projection", file=sys.stderr)
            # commit events + records but not projection
            try:
                run_git(["add", "events.jsonl", "state.json", "records", "inbox"], cwd=home, check=False)
                st = run_git(["status", "--porcelain"], cwd=home, check=False)
                if st.stdout.strip():
                    run_git(["commit", "-m", "merge %s (%d events) -> rev %d (crash before projection)" % (proposal_branch, len(re_chained), merge_rev)], cwd=home)
            except Exception:
                pass
            os._exit(1)

        # Regenerate projection
        try:
            from orda2.orda2_projection import write_projection
            write_projection(home)
        except Exception as e:
            print("projection failed: %s" % e, file=sys.stderr)
            raise

        # Commit everything
        try:
            run_git(["add", "events.jsonl", "state.json", "records", "inbox", "projection"], cwd=home, check=False)
            # also add proposal-conflicts etc if any leftover
            st = run_git(["status", "--porcelain"], cwd=home, check=False)
            if st.stdout.strip():
                run_git(["commit", "-m", "merge %s (%d events) -> rev %d" % (proposal_branch, len(re_chained), merge_rev)], cwd=home)
        except Exception as e:
            print("git commit after merge failed: %s" % e, file=sys.stderr)
            raise

        # verdict file
        cid = proposal_branch.replace("/", "-")
        os.makedirs(os.path.join(home, "reviews"), exist_ok=True)
        atomic_write(os.path.join(home, "reviews", "%s.json" % cid), json.dumps({"proposal": proposal_branch, "verdict": "accept", "merged_events": len(re_chained), "revision": merge_rev, "session": session}, indent=2, sort_keys=True) + "\n")
        try:
            run_git(["add", "reviews/%s.json" % cid], cwd=home, check=False)
            st = run_git(["status", "--porcelain", "reviews/%s.json" % cid], cwd=home, check=False)
            if st.stdout.strip():
                run_git(["commit", "-m", "verdict accept %s" % cid], cwd=home)
        except Exception:
            pass

    print(json.dumps({"merged": proposal_branch, "events": len(re_chained), "revision": merge_rev}, indent=2, sort_keys=True))
    return 0


def cmd_conflicts(args):
    home = require_home(args)
    pc_dir = os.path.join(home, "proposal-conflicts")
    if not os.path.isdir(pc_dir):
        print(json.dumps({"conflicts": []}, indent=2, sort_keys=True))
        return 0
    out = []
    for fn in sorted(os.listdir(pc_dir)):
        if fn.endswith(".json"):
            try:
                out.append(json.load(open(os.path.join(pc_dir, fn))))
            except Exception:
                pass
    # also print human block
    for c in out:
        print("CONFLICT %s vs main — record: %s" % (c.get("proposal"), c.get("slug")))
        print("  proposal base rev: %s (writer %s, %s)" % (c.get("base_rev"), c.get("proposal_writer") or "?", c.get("proposal_ts") or ""))
        print("  main rev:          %s (writer %s, %s)" % (c.get("main_rev"), c.get("main_writer") or "?", c.get("main_ts") or ""))
        print("  view both:  git show main:records/%s.json" % c.get("slug"))
        print("              git show %s:records/%s.json" % (c.get("proposal"), c.get("slug")))
        print("  resolve:    repropose on the newer rev, or decide as reviewer")
        print("")
    if not out:
        print("(no conflicts)")
    else:
        print(json.dumps({"conflicts": out}, indent=2, sort_keys=True))
    return 0


def cmd_brief(args):
    home = require_home(args)
    fmt = args.format or "md"
    if fmt == "json":
        from orda2.orda2_projection import build_brief
        brief = build_brief(home)
        print(json.dumps(brief, indent=2, sort_keys=True))
    else:
        # read projection/brief.md if exists, else build
        md_path = os.path.join(home, "projection", "brief.md")
        if os.path.exists(md_path):
            print(open(md_path).read(), end="")
        else:
            from orda2.orda2_projection import build_brief, render_markdown
            brief = build_brief(home)
            print(render_markdown(brief), end="")
    return 0


def cmd_verify(args):
    home = require_home(args)
    from orda2.orda2_store import Store
    store = Store(home)
    report = store.verify()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else EXIT_INTEGRITY


def cmd_reconcile(args):
    home = require_home(args)
    from orda2.orda2_store import Store
    store = Store(home)
    session = args.session or "reconcile"
    state = store.reconcile(session=session)
    print(json.dumps({"reconciled": True, "revision": state["revision"], "projects": len(state.get("projects", {}))}, indent=2, sort_keys=True))
    return 0


def cmd_export(args):
    home = require_home(args)
    from orda2.orda2_projection import build_brief
    brief = build_brief(home)
    bundle = {"kind": "orda-handoff", "schema_version": 2, "exported_utc": brief["generated_utc"], "from_session": brief["seat"].get("session"), "brief": brief}
    text = json.dumps(bundle, indent=2, sort_keys=True) + "\n"
    if args.out:
        out = os.path.abspath(os.path.expanduser(args.out))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w") as f:
            f.write(text)
        print(json.dumps({"exported": out, "bytes": len(text)}, indent=2, sort_keys=True))
    else:
        print(text, end="")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="orda2")
    ap.add_argument("--home", default=DEFAULT_HOME, help="state home")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init")
    p.add_argument("--home", dest="home", default=DEFAULT_HOME)
    p.add_argument("--from-v1", dest="from_v1", default=None)
    p.set_defaults(fn=lambda a: cmd_init(a))

    p = sub.add_parser("worktree")
    p.add_argument("--home", dest="home", default=DEFAULT_HOME)
    # subcommand new
    p.add_argument("worktree_cmd", choices=["new"])
    p.add_argument("--session", required=True)
    p.set_defaults(fn=lambda a: cmd_worktree_new(a) if a.worktree_cmd=="new" else None)

    p = sub.add_parser("propose")
    p.add_argument("--home", dest="home", default=DEFAULT_HOME)
    p.add_argument("--session", required=True)
    p.add_argument("--file", default=None)
    p.add_argument("--rebase", action="store_true")
    p.add_argument("--note", default="")
    p.set_defaults(fn=cmd_propose)

    p = sub.add_parser("ingest")
    p.add_argument("--home", dest="home", default=DEFAULT_HOME)
    p.add_argument("--session", required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--batch", default=None)
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("seat")
    p.add_argument("--home", dest="home", default=DEFAULT_HOME)
    p.add_argument("action", choices=["claim","renew","release"])
    p.add_argument("--session", required=True)
    p.add_argument("--ttl", type=int, default=900)
    p.add_argument("--steal", action="store_true")
    p.set_defaults(fn=cmd_seat)

    p = sub.add_parser("review")
    p.add_argument("--home", dest="home", default=DEFAULT_HOME)
    p.add_argument("--wait", action="store_true")
    p.add_argument("--timeout", type=int, default=30)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_review)

    p = sub.add_parser("merge")
    p.add_argument("--home", dest="home", default=DEFAULT_HOME)
    p.add_argument("--session", required=True)
    p.add_argument("--proposal", required=True)
    p.set_defaults(fn=cmd_merge)

    p = sub.add_parser("conflicts")
    p.add_argument("--home", dest="home", default=DEFAULT_HOME)
    p.set_defaults(fn=cmd_conflicts)

    p = sub.add_parser("brief")
    p.add_argument("--home", dest="home", default=DEFAULT_HOME)
    p.add_argument("--format", choices=["md","json"], default="md")
    p.set_defaults(fn=cmd_brief)

    p = sub.add_parser("verify")
    p.add_argument("--home", dest="home", default=DEFAULT_HOME)
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("reconcile")
    p.add_argument("--home", dest="home", default=DEFAULT_HOME)
    p.add_argument("--session", default=None)
    p.set_defaults(fn=cmd_reconcile)

    p = sub.add_parser("export")
    p.add_argument("--home", dest="home", default=DEFAULT_HOME)
    p.add_argument("--out", default=None)
    p.set_defaults(fn=cmd_export)

    args = ap.parse_args(argv)
    # dispatch: handle home override style (argparse already does)
    # But init parser defined --home twice; ensure args.home present
    if not hasattr(args, 'home') or not args.home:
        args.home = DEFAULT_HOME
    try:
        result = args.fn(args)
        return result if isinstance(result, int) else 0
    except StoreError as e:
        print("ERROR[%d]: %s" % (e.code, e), file=sys.stderr)
        return e.code
    except SystemExit:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
