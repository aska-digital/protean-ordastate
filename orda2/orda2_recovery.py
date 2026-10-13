"""Durable merge intents. Caller holds the store lock for every operation.

The intent digest commits to proposal bytes and preimages, before publication.
Events carry that digest; record rev/head_event are derived afterwards, avoiding
an event-hash/payload-hash cycle. Retained intents are integrity evidence.
"""
import hashlib
import json
import os
import re

from orda2.orda2_events import ZERO_HASH, event_hash
from orda2.orda2_store import StoreError, EXIT_INTEGRITY, atomic_write


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def file_digest(path):
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def text_digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def fail(message):
    raise StoreError(EXIT_INTEGRITY, "recovery integrity failure: " + message)


def valid_path(path):
    if not isinstance(path, str):
        return False
    parts = path.split("/")
    if len(parts) < 2 or parts[0] not in ("records", "inbox"):
        return False
    if parts[0] == "records" and len(parts) != 2:
        return False
    return (parts[-1].endswith(".json") and all(part not in (".", "..") and
            re.fullmatch(r"[a-zA-Z0-9._-]+", part) for part in parts[1:]))


def assert_safe_path(home, path):
    if not valid_path(path):
        fail("invalid intent file path")
    current = home
    for part in path.split("/"):
        current = os.path.join(current, part)
        if os.path.islink(current):
            fail("symlink in recovery path %s" % path)


def build_events(intent, identity):
    prev = intent["base_hash"]
    events = []
    for idx, template in enumerate(intent["templates"]):
        ev = dict(template)
        ev.update(seq=intent["base_seq"] + idx + 1,
                  revision=intent["base_revision"] + idx + 1,
                  prev_hash=prev, recovery_id=identity)
        ev.pop("hash", None)
        ev["hash"] = event_hash(prev, ev)
        events.append(ev)
        prev = ev["hash"]
    return events


def targets(intent, events):
    if (not events or events[-1].get("kind") != "merge" or events[-1].get("project") is not None or
            events[-1].get("data", {}).get("proposal") != intent["proposal"] or
            events[-1].get("data", {}).get("merged_events") != len(events) - 1 or
            any(e.get("kind") not in ("update", "ingest") or not isinstance(e.get("data"), dict) for e in events[:-1])):
        fail("invalid transaction event templates")
    result = {}
    record_slugs = {p[8:-5] for p in intent["files"] if p.startswith("records/")}
    event_slugs = [e.get("project") for e in events if e.get("kind") == "update"]
    if len(event_slugs) != len(set(event_slugs)) or set(event_slugs) != record_slugs:
        fail("update events and record payloads do not correspond")
    for path, entry in intent["files"].items():
        text = entry["proposed"]
        if path.startswith("records/"):
            slug = path[8:-5]
            rec = json.loads(text)
            if not isinstance(rec, dict) or rec.get("slug") != slug:
                fail("record slug does not match intent path %s" % path)
            matching = [e for e in events if e.get("project") == slug and e.get("kind") == "update"]
            if len(matching) != 1:
                fail("intent needs exactly one update for %s" % path)
            ev = matching[0]
            if any(digest(rec.get(k)) != digest(v) for k, v in (ev.get("data") or {}).items()):
                fail("update metadata does not match proposed payload %s" % path)
            rec.update(rev=ev["revision"], head_event=ev["hash"])
            text = json.dumps(rec, indent=2, sort_keys=True) + "\n"
        result[path] = text
    return result


def prepare(home, proposal, main_events, templates, files):
    base = main_events[-1] if main_events else {}
    intent = {"version": 1, "proposal": proposal, "base_seq": base.get("seq", 0),
              "base_hash": base.get("hash", ZERO_HASH), "base_revision": base.get("revision", 0),
              "templates": templates, "files": files}
    for path in files:
        assert_safe_path(home, path)
    identity = digest(intent)
    events = build_events(intent, identity)
    # Validate payloads before publishing either intent or events.
    targets(intent, events)
    directory = os.path.join(home, "recovery")
    if os.path.islink(directory):
        fail("symlink in recovery evidence")
    if not os.path.isdir(directory):
        os.mkdir(directory)
        from orda2.orda2_store import fsync_directory
        fsync_directory(home)
    atomic_write(os.path.join(directory, identity + ".json"),
                 json.dumps({"intent": intent, "events": events}, indent=2, sort_keys=True) + "\n")
    return events, targets(intent, events)


def published(home, events):
    """Validate every published intent and its complete contiguous segment."""
    identities = {e["recovery_id"] for e in events if "recovery_id" in e}
    journals = []
    for identity in identities:
        try:
            if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
                fail("invalid recovery identity")
            if os.path.islink(os.path.join(home, "recovery")) or os.path.islink(os.path.join(home, "recovery", identity + ".json")):
                fail("symlink in recovery evidence")
            with open(os.path.join(home, "recovery", identity + ".json")) as f:
                journal = json.load(f)
            intent = journal["intent"]
            if intent["version"] != 1 or digest(intent) != identity:
                fail("intent digest mismatch %s" % identity)
            if (any(type(intent[k]) is not int or intent[k] < 0 for k in ("base_seq", "base_revision")) or
                    not isinstance(intent["base_hash"], str) or not re.fullmatch(r"[0-9a-f]{64}", intent["base_hash"]) or
                    not isinstance(intent["proposal"], str) or not isinstance(intent["files"], dict) or
                    not isinstance(intent["templates"], list) or not intent["templates"] or
                    any(not isinstance(e, dict) for e in intent["templates"])):
                fail("invalid intent contents")
            for path, entry in intent["files"].items():
                assert_safe_path(home, path)
                if not valid_path(path) or not isinstance(entry["proposed"], str):
                    fail("invalid intent file path or bytes")
                if path.startswith("records/") and (not isinstance(entry.get("before_state_sha256"), str) or
                        not re.fullmatch(r"[0-9a-f]{64}", entry["before_state_sha256"])):
                    fail("invalid prior state payload digest")
                before = entry["before_sha256"]
                if before is not None and (not isinstance(before, str) or not re.fullmatch(r"[0-9a-f]{64}", before)):
                    fail("invalid preimage digest")
            expected = build_events(intent, identity)
            if expected != journal["events"]:
                fail("journal event mismatch %s" % identity)
            start = intent["base_seq"]
            base_hash = events[start-1]["hash"] if start else ZERO_HASH
            base_revision = events[start-1]["revision"] if start else 0
            if base_revision != intent["base_revision"] or base_hash != intent["base_hash"] or events[start:start + len(expected)] != expected:
                fail("published segment mismatch %s" % identity)
            if [e for e in events if e.get("recovery_id") == identity] != expected:
                fail("ambiguous published segment %s" % identity)
            journals.append((intent, expected, targets(intent, expected)))
        except StoreError:
            raise
        except Exception as e:
            fail("missing or corrupt intent %s (%s)" % (identity, e))
    return sorted(journals, key=lambda j: j[0]["base_seq"])


def latest_targets(journals):
    latest = {}
    for intent, events, payloads in journals:
        for path, text in payloads.items():
            latest[path] = (intent["files"][path]["before_sha256"], text, events[-1]["seq"])
    return latest


def recover_files(home, journals, events):
    """Preflight all writes first; never overwrite an unrelated later value."""
    writes = []
    for path, (before, text, seq) in latest_targets(journals).items():
        # A later legacy write has no complete payload evidence. Refuse replay.
        slug = path[8:-5] if path.startswith("records/") else None
        if slug and any(e["seq"] > seq and e.get("project") == slug and
                        e.get("kind") in ("update", "project-upsert", "project-remove") for e in events):
            fail("later write without recovery evidence for %s" % path)
        current = file_digest(os.path.join(home, path))
        if current == text_digest(text):
            continue
        if current != before:
            fail("unrelated or corrupt current file %s; refusing overwrite" % path)
        writes.append((path, text))
    for idx, (path, text) in enumerate(writes, 1):
        atomic_write(os.path.join(home, path), text)
        if os.environ.get("ORDA2_CRASH_AFTER") == "recovery-file-%d" % idx:
            os._exit(1)
