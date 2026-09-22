#!/usr/bin/env python3
"""Migration from v1 snapshot → v2 git repo."""
import hashlib
import json
import os
import shutil

from orda2.orda2_store import atomic_write, ensure_git_repo, run_git, utcnow
from orda2.orda2_events import ZERO_HASH, event_hash, read_events


def init_home(home, from_v1=None):
    """Create v2 repo at home; optionally import v1 snapshot."""
    os.makedirs(home, exist_ok=True)
    ensure_git_repo(home)
    from orda2.orda2_store import Store
    store = Store(home)
    store.ensure_dirs()

    # Runtime debris that must never enter the store's git surface.
    gi_path = os.path.join(home, ".gitignore")
    if not os.path.exists(gi_path):
        atomic_write(gi_path, "worktrees/\n.lock\n")

    # If already initialized, idempotent
    if os.path.exists(os.path.join(home, "state.json")) and os.path.exists(os.path.join(home, "events.jsonl")):
        # already done, but ensure git has files
        return {"home": home, "already": True}

    v1_state = None
    v1_events = []
    v1_head_hash = ZERO_HASH
    v1_revision = 0
    v1_count = 0

    if from_v1:
        v1_state_path = os.path.join(from_v1, "state.json")
        v1_events_path = os.path.join(from_v1, "events.jsonl")
        if os.path.exists(v1_state_path):
            with open(v1_state_path) as f:
                v1_state = json.load(f)
            v1_revision = v1_state.get("revision", 0)
        if os.path.exists(v1_events_path):
            v1_events = read_events(v1_events_path)
            v1_count = len(v1_events)
            if v1_events:
                v1_head_hash = v1_events[-1]["hash"]
        # copy archive
        archive_dir = os.path.join(home, "archive")
        os.makedirs(archive_dir, exist_ok=True)
        if os.path.exists(v1_events_path):
            shutil.copy2(v1_events_path, os.path.join(archive_dir, "v1-events.jsonl"))

    # Build records from v1 state or v1 events
    projects = {}
    if v1_state and "projects" in v1_state:
        projects = v1_state["projects"]
    elif v1_events:
        # replay v1 events: project-upsert etc
        tmp_projects = {}
        for e in v1_events:
            if e.get("kind") == "project-upsert" and e.get("project"):
                rec = dict(tmp_projects.get(e["project"]) or {})
                rec.update({"slug": e["project"]})
                for field in ("status", "summary", "next", "detail_ref", "tag"):
                    if field in (e.get("data") or {}):
                        rec[field] = e["data"][field]
                tmp_projects[e["project"]] = rec
        projects = tmp_projects

    # Write records/<slug>.json.
    # rev is seeded to the v1 revision that last touched each record (the
    # per-record CAS base); head_event is that event's hash. Records never
    # touched by a project event fall back to the v1 head revision/hash.
    last_rev = {}
    last_hash = {}
    for e in v1_events:
        if e.get("project"):
            last_rev[e["project"]] = int(e.get("revision", 0) or 0)
            if e.get("hash"):
                last_hash[e["project"]] = e["hash"]
    record_count = 0
    import_map = {}
    for slug, rec in sorted(projects.items()):
        # enrich with rev/head_event
        enriched = dict(rec)
        enriched["slug"] = slug
        enriched["rev"] = last_rev.get(slug) or (int(v1_revision) if v1_revision else 1)
        # find last event touching this slug
        head = last_hash.get(slug, v1_head_hash)
        enriched["head_event"] = head
        enriched["updated_utc"] = enriched.get("updated_utc") or utcnow()
        enriched["writer"] = enriched.get("writer") or enriched.get("last_writer") or "v1-import"
        # ensure required fields
        enriched.setdefault("status", "active")
        enriched.setdefault("summary", "")
        rec_path = os.path.join(home, "records", "%s.json" % slug)
        atomic_write(rec_path, json.dumps(enriched, indent=2, sort_keys=True) + "\n")
        import_map[slug] = hashlib.sha256(json.dumps(enriched, sort_keys=True).encode()).hexdigest()[:16]
        record_count += 1

    # Build state.json
    now = utcnow()
    # revision for v2 = v1_revision (so chain continuity) ; if no v1, 0 or record_count?
    revision = int(v1_revision) if v1_revision else record_count
    state = {
        "schema_version": 2,
        "revision": revision,
        "epoch": 1,
        "updated_utc": now,
        "writer": "genesis",
        "projects": {}
    }
    # load back records into state projects
    for slug in projects:
        with open(os.path.join(home, "records", "%s.json" % slug)) as f:
            state["projects"][slug] = json.load(f)

    atomic_write(os.path.join(home, "state.json"), json.dumps(state, indent=2, sort_keys=True) + "\n")

    # Build events.jsonl: copy v1 events + genesis event
    events_path = os.path.join(home, "events.jsonl")
    if v1_events:
        # write v1 events verbatim (they already have chain)
        with open(events_path, "w") as f:
            for e in v1_events:
                f.write(json.dumps(e, sort_keys=True) + "\n")
        prev = v1_events[-1]["hash"]
        seq = v1_events[-1]["seq"] + 1
    else:
        prev = ZERO_HASH
        seq = 1
        open(events_path, "w").close()

    genesis = {
        "seq": seq,
        "prev_hash": prev,
        "revision": revision + (0 if v1_events else 0),
        "ts": now,
        "writer": "genesis",
        "kind": "genesis",
        "project": None,
        "data": {"v1_head_hash": v1_head_hash, "v1_revision": v1_revision, "v1_event_count": v1_count, "record_count": record_count},
        "note": "v2 genesis from %s" % (from_v1 or "empty"),
    }
    # compute hash
    tmp = dict(genesis)
    tmp.pop("hash", None)
    genesis["hash"] = event_hash(prev, tmp)
    # if v1_events present, revision stays same? spec says append genesis with v1_revision etc, keep revision = v1_revision ?
    # We set genesis revision = revision (same) so state revision stays consistent. If no v1, revision = record_count.
    # Append
    with open(events_path, "a") as f:
        f.write(json.dumps(genesis, sort_keys=True) + "\n")

    # If v1 present, state revision already set to v1_revision; genesis does not bump revision (bridge event)
    # Actually if we appended genesis, need to handle revision: keep state revision = v1_revision, genesis revision = v1_revision
    # so verify state revision == last event revision holds.

    # Write seat.json
    seat = {"session": None, "holder": None, "acquired_utc": None, "renew_utc": None, "expires_utc": None, "ttl_s": 900, "epoch": 1, "note": "initialized"}
    atomic_write(os.path.join(home, "seat.json"), json.dumps(seat, indent=2, sort_keys=True) + "\n")

    # Write archive import map
    atomic_write(os.path.join(home, "archive", "v1-import.json"), json.dumps({"imported_utc": now, "record_count": record_count, "map": import_map, "v1_head_hash": v1_head_hash, "v1_revision": v1_revision}, indent=2, sort_keys=True) + "\n")

    # Projection
    try:
        from orda2.orda2_projection import write_projection
        from orda2.orda2_store import Store as S2
        write_projection(home)
    except Exception as e:
        pass

    # Git commit genesis
    try:
        run_git(["add", "."], cwd=home)
        run_git(["commit", "-m", "genesis: v2 import %d records from %s (rev %s)" % (record_count, from_v1 or "empty", v1_revision)], cwd=home)
    except Exception:
        pass

    return {"home": home, "record_count": record_count, "v1_revision": v1_revision, "v1_head_hash": v1_head_hash}
