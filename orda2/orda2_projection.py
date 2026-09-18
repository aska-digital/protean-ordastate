#!/usr/bin/env python3
"""Projection: pointer-only brief regeneration, stdlib only."""
import json
import os

from orda2.orda2_events import read_events

STATUS_ORDER = ["active", "running", "awaiting-owner", "blocked", "paused", "followup", "closed"]


def _utcnow():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_brief(home):
    """Build brief dict from state.json + seat.json + events + conflicts."""
    state_path = os.path.join(home, "state.json")
    seat_path = os.path.join(home, "seat.json")
    events_path = os.path.join(home, "events.jsonl")

    with open(state_path) as f:
        state = json.load(f)
    seat = {}
    if os.path.exists(seat_path):
        with open(seat_path) as f:
            seat = json.load(f)

    events = read_events(events_path)
    conflicts = [e for e in events if e.get("kind") in ("conflict",)]  # last 10

    # also read proposal-conflicts files
    conflict_blocks = []
    pc_dir = os.path.join(home, "proposal-conflicts")
    if os.path.isdir(pc_dir):
        for fn in sorted(os.listdir(pc_dir)):
            if fn.endswith(".json"):
                try:
                    conflict_blocks.append(json.load(open(os.path.join(pc_dir, fn))))
                except Exception:
                    pass

    brief = {
        "generated_utc": _utcnow(),
        "home": home,
        "revision": state.get("revision", 0),
        "epoch": seat.get("epoch", 1),
        "seat": {k: seat.get(k) for k in ("session", "expires_utc", "valid_for_writes", "epoch")},
        "updated_utc": state.get("updated_utc"),
        "last_writer": state.get("writer"),
        "records": {},
        "unresolved_conflicts": conflict_blocks[-10:],
        "recent_conflict_events": conflicts[-10:],
        "references": {
            "events": events_path,
            "state": state_path,
            "last_event_seq": events[-1]["seq"] if events else 0,
        },
    }
    # compact list of records
    for slug, rec in sorted(state.get("projects", {}).items()):
        brief["records"][slug] = {
            "slug": slug,
            "status": rec.get("status"),
            "summary": rec.get("summary"),
            "rev": rec.get("rev"),
            "writer": rec.get("writer"),
        }
    return brief


def render_markdown(brief):
    lines = [
        "# Orda Live Brief (projection — generated; do not hand-edit)",
        "",
        "revision %s | epoch %s | seat %s (expires %s) | last writer %s" % (
            brief["revision"], brief["epoch"],
            brief["seat"].get("session") or "unclaimed",
            brief["seat"].get("expires_utc") or "-",
            brief["last_writer"] or "-"
        ),
        "",
    ]
    if brief["records"]:
        lines.append("## Records (%d)" % len(brief["records"]))
        for slug, rec in sorted(brief["records"].items()):
            lines.append("- %s [rev %s, %s]: %s" % (slug, rec.get("rev"), rec.get("status"), rec.get("summary") or ""))
        lines.append("")

    if brief.get("unresolved_conflicts"):
        lines.append("## Unresolved conflicts")
        for c in brief["unresolved_conflicts"]:
            lines.append("- %s" % json.dumps(c, sort_keys=True))
        lines.append("")
    # render conflict events too in the canonical block format if any
    # The spec requires exact block in brief.md for conflict refusal visibility.
    # We write per-conflict files; also render the standard block from proposal-conflicts/*.json
    for c in brief.get("unresolved_conflicts", []):
        # c is the proposal-conflicts file content: {proposal, slug, base_rev, main_rev, ...}
        # Render the required human-visible block verbatim if fields present
        if "proposal" in c and "slug" in c:
            lines.append("CONFLICT %s vs main — record: %s" % (c.get("proposal"), c.get("slug")))
            lines.append("  proposal base rev: %s (writer %s, %s)" % (c.get("base_rev"), c.get("proposal_writer") or c.get("session") or "?", c.get("proposal_ts") or ""))
            lines.append("  main rev:          %s (writer %s, %s)" % (c.get("main_rev"), c.get("main_writer") or "?", c.get("main_ts") or ""))
            lines.append("  view both:  git show main:records/%s.json" % c.get("slug"))
            lines.append("              git show %s:records/%s.json" % (c.get("proposal"), c.get("slug")))
            lines.append("  resolve:    repropose on the newer rev, or decide as reviewer")
            lines.append("")
    if brief.get("recent_conflict_events"):
        # fallback if no proposal-conflicts files but conflict events exist
        if not brief.get("unresolved_conflicts"):
            lines.append("## Unresolved conflicts")
            for e in brief["recent_conflict_events"]:
                lines.append("- seq %s conflict on %s by %s" % (e.get("seq"), e.get("project"), e.get("writer")))
            lines.append("")

    lines.append("Detail: events.jsonl (through seq %s) — pointers only, no payload copied." % brief["references"]["last_event_seq"])
    return "\n".join(lines) + "\n"


def write_projection(home, brief=None):
    brief = brief or build_brief(home)
    pj = os.path.join(home, "projection")
    os.makedirs(pj, exist_ok=True)
    from orda2.orda2_store import atomic_write
    atomic_write(os.path.join(pj, "brief.json"), json.dumps(brief, indent=2, sort_keys=True) + "\n")
    atomic_write(os.path.join(pj, "brief.md"), render_markdown(brief))
    return brief
