#!/usr/bin/env python3
"""Event chain, segment handling, verify / reconcile — stdlib only."""
import hashlib
import json
import os

ZERO_HASH = "0" * 64


def event_hash(prev_hash, event):
    payload = dict(event)
    payload.pop("hash", None)
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev_hash + blob).encode()).hexdigest()


def read_events(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for idx, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                # Surface as a corrupt-line error that verify() will treat as integrity failure (exit 5).
                # We raise a ValueError with a structured message so callers can map to exit 5 without traceback.
                raise ValueError("corrupt events.jsonl line %d: unparseable JSON (%s) — integrity failure" % (idx, e.msg))
            except Exception as e:
                raise ValueError("corrupt events.jsonl line %d: %s — integrity failure" % (idx, e))
    return out


def write_events(path, events):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        for e in events:
            f.write(json.dumps(e, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp, path)


def append_event(path, event_dict_without_hash):
    """Append one event extending chain; returns event with hash/seq filled."""
    events = read_events(path)
    prev = events[-1]["hash"] if events else ZERO_HASH
    seq = (events[-1]["seq"] + 1) if events else 1
    event = dict(event_dict_without_hash)
    event["seq"] = seq
    event["prev_hash"] = prev
    event["hash"] = event_hash(prev, event)
    with open(path, "a") as f:
        f.write(json.dumps(event, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return event


def verify_chain(events):
    problems = []
    prev = ZERO_HASH
    expected_seq = 1
    required_fields = ["seq", "prev_hash", "hash", "revision", "ts", "writer", "kind"]
    for idx, e in enumerate(events, start=1):
        seq = e.get("seq")
        # required-field presence
        for field in required_fields:
            if field not in e or e.get(field) is None:
                problems.append("line %d seq %s: missing required field '%s' — integrity failure" % (idx, seq, field))
        # monotonic seq check
        if seq != expected_seq:
            problems.append("line %d: seq mismatch expected %d got %s (chain broken) — integrity failure" % (idx, expected_seq, seq))
        if e.get("prev_hash") != prev:
            problems.append("seq %s line %d: prev_hash mismatch (chain broken) — integrity failure" % (seq, idx))
        try:
            exp = event_hash(prev, e)
        except Exception as ex:
            problems.append("seq %s line %d: hash computation failed (%s) — integrity failure" % (seq, idx, ex))
            exp = None
        if exp is not None and e.get("hash") != exp:
            problems.append("seq %s line %d: hash mismatch (event tampered or corrupt) — integrity failure" % (seq, idx))
        # advance chain: use recorded hash (even if tampered) for next prev check so subsequent prev_hash mismatches are also reported
        h = e.get("hash")
        if h:
            prev = h
        expected_seq += 1
    return problems


def verify_store(home, events_path=None, state_path=None):
    """Full verify: chain + state revision consistency."""
    events_path = events_path or os.path.join(home, "events.jsonl")
    state_path = state_path or os.path.join(home, "state.json")
    problems = []
    try:
        events = read_events(events_path)
    except ValueError as e:
        return {"ok": False, "problems": [str(e)], "events": 0}
    except Exception as e:
        return {"ok": False, "problems": ["events read failed: %s" % e], "events": 0}
    problems.extend(verify_chain(events))
    if os.path.exists(state_path):
        try:
            with open(state_path) as f:
                state = json.load(f)
        except json.JSONDecodeError as e:
            problems.append("state.json corrupt: unparseable JSON (%s) — integrity failure" % e.msg)
            return {"ok": False, "problems": problems, "events": len(events)}
        except Exception as e:
            problems.append("state.json read failed: %s — integrity failure" % e)
            return {"ok": False, "problems": problems, "events": len(events)}
        last_rev = events[-1]["revision"] if events else 0
        # v2: state revision tracks event count / last revision
        if state.get("revision", 0) < last_rev:
            problems.append("state revision %s behind event log revision %s (crash window; run reconcile)" % (state.get("revision"), last_rev))
        if state.get("revision", 0) > last_rev:
            problems.append("state revision %s ahead of event log %s" % (state.get("revision"), last_rev))
        # also check projection age if exists
        pj = os.path.join(home, "projection", "brief.json")
        if os.path.exists(pj):
            try:
                brief = json.load(open(pj))
                if brief.get("revision") != state.get("revision"):
                    problems.append("projection brief.json at revision %s, state at %s (stale projection; run: reconcile)" % (brief.get("revision"), state.get("revision")))
            except Exception:
                pass
    return {"ok": not problems, "problems": problems, "events": len(events)}


def rechain_segment(segment_events, new_prev_hash, start_seq):
    """Re-chain a proposal segment onto a new prev_hash and seq base."""
    out = []
    prev = new_prev_hash
    seq = start_seq
    for e in segment_events:
        ne = dict(e)
        ne["prev_hash"] = prev
        ne["seq"] = seq
        # recompute hash without old hash
        ne.pop("hash", None)
        # need to recompute from payload
        h = event_hash(prev, ne)
        ne["hash"] = h
        out.append(ne)
        prev = h
        seq += 1
    return out
