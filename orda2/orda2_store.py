#!/usr/bin/env python3
"""Git ops, seat lease, state materialization — stdlib + git."""
import datetime
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile

from orda2.orda2_events import ZERO_HASH, event_hash, read_events, verify_chain, write_events, rechain_segment

SCHEMA_VERSION = 2
EXIT_OK, EXIT_CONFLICT, EXIT_LEASE, EXIT_INTEGRITY, EXIT_USAGE = 0, 3, 4, 5, 2
DEFAULT_TTL_S = 900


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(s):
    return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)


class StoreError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def atomic_write(path, text):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


class Lock:
    def __init__(self, home):
        self.path = os.path.join(home, ".lock")

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)
        return False


def run_git(args, cwd, check=True):
    result = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise StoreError(EXIT_INTEGRITY, "git %s failed: %s %s" % (" ".join(args), result.stderr.strip(), result.stdout.strip()))
    return result


def git_head(cwd):
    r = run_git(["rev-parse", "HEAD"], cwd=cwd)
    return r.stdout.strip()


def git_head_file(path, cwd, ref="HEAD"):
    # if file doesn't exist at HEAD, return None
    r = run_git(["show", "%s:%s" % (ref, path)], cwd=cwd, check=False)
    if r.returncode != 0:
        return None
    return r.stdout


def ensure_git_repo(home):
    if not os.path.isdir(os.path.join(home, ".git")):
        run_git(["init", "-b", "main"], cwd=home)
        run_git(["config", "user.email", "orda2@local"], cwd=home)
        run_git(["config", "user.name", "orda2"], cwd=home)
        run_git(["config", "rerere.enabled", "true"], cwd=home)


def slug_valid(slug):
    return bool(re.match(r"^[a-z0-9][a-z0-9._-]{1,63}$", slug))


# ---------- Secrets scan (attack 7) ----------
_SECRET_PATTERNS = [
    (re.compile(r'xox[baprs]-[A-Za-z0-9\-_]+'), 'slack-token (xox*)'),
    (re.compile(r'sk-[A-Za-z0-9\-_]{6,}'), 'openai-key (sk-)'),
    (re.compile(r'ghp_[A-Za-z0-9]{6,}'), 'github-pat (ghp_)'),
    (re.compile(r'github_pat_[A-Za-z0-9_]{6,}'), 'github-pat (github_pat_)'),
    (re.compile(r'AKIA[0-9A-Z]{6,}'), 'aws-key (AKIA)'),
    (re.compile(r'Bearer\s+[A-Za-z0-9\-_\.=]{6,}'), 'bearer-token (Bearer )'),
]
_GENERIC_KEY_RE = re.compile(r'(?i)(api[_-]?key|secret|token)')


def scan_for_secrets(text, filename=""):
    """Scan text for secret patterns. Returns pattern label or None. Value is redacted."""
    if not isinstance(text, str):
        text = json.dumps(text)
    for pat, label in _SECRET_PATTERNS:
        if pat.search(text):
            return label
    # generic key-name + high-entropy value detection
    # Try JSON parsing first for precise key matching
    try:
        obj = json.loads(text) if text.strip().startswith(("{", "[")) else None
    except Exception:
        obj = None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if _GENERIC_KEY_RE.search(k) and isinstance(v, str) and len(v) >= 16 and re.search(r'[A-Za-z0-9\-_\.=]{12,}', v):
                return 'generic-secret (api_key/token/secret)'
            if isinstance(v, (dict, list)):
                # shallow recurse one level for nested structures
                if isinstance(v, dict):
                    for kk, vv in v.items():
                        if _GENERIC_KEY_RE.search(kk) and isinstance(vv, str) and len(vv) >= 16:
                            return 'generic-secret (api_key/token/secret)'
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                for k, v in item.items():
                    if _GENERIC_KEY_RE.search(k) and isinstance(v, str) and len(v) >= 16:
                        return 'generic-secret (api_key/token/secret)'
    # raw-text fallback for non-JSON or inline key=value
    if re.search(r'(?i)(api[_-]?key|secret|token)\s*[:=]\s*["\']?[A-Za-z0-9\-_\.=]{16,}', text):
        return 'generic-secret (api_key/token/secret)'
    # also catch JSON key with quoted high-entropy value even if not parsed as above
    if _GENERIC_KEY_RE.search(text) and re.search(r'[:=]\s*["\']?[A-Za-z0-9\-_\.=]{16,}["\']?', text):
        # only trigger if there's a plausible high-entropy token nearby (avoid false positives on short words)
        # check that the matched value segment has length >=16
        # we already matched generic pattern above; be conservative: require at least 16-char alnum segment
        m = re.search(r'(?i)(api[_-]?key|secret|token)[^A-Za-z0-9]*[:=][^A-Za-z0-9]*["\']?([A-Za-z0-9\-_\.=]{16,})', text)
        if m:
            return 'generic-secret (api_key/token/secret)'
    return None


def assert_no_secrets(text, filename):
    label = scan_for_secrets(text, filename)
    if label:
        raise StoreError(EXIT_USAGE, "secrets scan hit in %s: pattern %s — value redacted (refused)" % (filename, label))


# ---------- Main cleanliness (attack 8) ----------
def check_main_clean(home):
    """Verify main checkout has no untracked/modified records/, events/, state/. Refuse if dirty."""
    # Use git status --porcelain for the relevant paths
    r = run_git(["status", "--porcelain", "--", "records", "events.jsonl", "state.json", "inbox", "projection"], cwd=home, check=False)
    out = r.stdout.strip()
    if out:
        # Show first few lines, redact full content but name the paths
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        preview = "; ".join(lines[:3])
        if len(lines) > 3:
            preview += " (+%d more)" % (len(lines) - 3)
        raise StoreError(EXIT_USAGE, "main is dirty (%s) — direct writes detected; main must be clean (no untracked/modified records/, events/, state/) before mutating operations; discard or commit debris then retry" % preview)


# ---------- Store/host boundary (J13) ----------
def check_store_boundary(home):
    """Fail (message) when an ancestor git repo sees the store path.

    Returns None when the boundary holds: either no ancestor repo exists, or
    the ancestor's `git status --porcelain -- <relpath>` is clean (the ignore
    rule is in force). Returns a problem string otherwise (verify exit 5).
    """
    home = os.path.abspath(home)
    cur = os.path.dirname(home)
    for _ in range(8):
        if os.path.isdir(os.path.join(cur, ".git")):
            rel = os.path.relpath(home, cur)
            if rel.startswith(".."):
                return None
            r = run_git(["status", "--porcelain", "--", rel], cwd=cur, check=False)
            if r.returncode != 0:
                return None  # cannot prove a breach; other checks own failures
            lines = [l for l in r.stdout.splitlines() if l.strip()]
            if lines:
                preview = "; ".join(l.strip() for l in lines[:3])
                return ("store/host boundary breach (J13): enclosing repo tracks the store path "
                        "(%s) — the ignore rule is missing or regressed; "
                        "verify exit 5 until the enclosing repo ignores it" % preview)
            return None
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent
    return None


# ---------- Store class (main worktree) ----------
class Store:
    def __init__(self, home):
        self.home = os.path.abspath(os.path.expanduser(home))
        self.state_path = os.path.join(self.home, "state.json")
        self.events_path = os.path.join(self.home, "events.jsonl")
        self.seat_path = os.path.join(self.home, "seat.json")

    def exists(self):
        return os.path.exists(self.state_path) and os.path.isdir(os.path.join(self.home, ".git"))

    def ensure_dirs(self):
        os.makedirs(self.home, exist_ok=True)
        os.makedirs(os.path.join(self.home, "records"), exist_ok=True)
        os.makedirs(os.path.join(self.home, "inbox"), exist_ok=True)
        os.makedirs(os.path.join(self.home, "projection"), exist_ok=True)
        os.makedirs(os.path.join(self.home, "proposal-conflicts"), exist_ok=True)
        os.makedirs(os.path.join(self.home, "reviews"), exist_ok=True)
        os.makedirs(os.path.join(self.home, "archive"), exist_ok=True)

    # ---- state / events / records ----
    def load_state(self):
        with open(self.state_path) as f:
            return json.load(f)

    def load_seat(self):
        if not os.path.exists(self.seat_path):
            return {"session": None, "expires_utc": None, "epoch": 1, "ttl_s": DEFAULT_TTL_S}
        with open(self.seat_path) as f:
            return json.load(f)

    def read_events(self):
        return read_events(self.events_path)

    def seat_status(self, now=None):
        seat = self.load_seat()
        seat["valid_for_writes"] = False
        if seat.get("session") and seat.get("expires_utc"):
            now = now or utcnow()
            try:
                seat["valid_for_writes"] = parse_utc(now) < parse_utc(seat["expires_utc"])
            except Exception:
                seat["valid_for_writes"] = False
        return seat

    def require_seat(self, session):
        seat = self.seat_status()
        if not seat["valid_for_writes"] or seat.get("session") != session:
            raise StoreError(EXIT_LEASE, "no live merge seat for session %r (seat=%r expires=%r) — only the seat may merge" % (session, seat.get("session"), seat.get("expires_utc")))
        return seat

    # ---- seat lease ----
    def claim(self, session, ttl_s=DEFAULT_TTL_S, steal=False):
        with Lock(self.home):
            return self._claim_locked(session, ttl_s, steal)

    def _claim_locked(self, session, ttl_s, steal):
        now = utcnow()
        seat = self.load_seat()
        prev_holder = seat.get("session")
        epoch = seat.get("epoch", 1)
        takeover = None
        if seat.get("session"):
            live = False
            try:
                live = parse_utc(now) < parse_utc(seat.get("expires_utc") or now)
            except Exception:
                live = False
            if live and seat["session"] == session:
                takeover = "renew-on-claim"
            elif live and not steal:
                raise StoreError(EXIT_LEASE, "seat held by %s until %s (use --steal to force a recorded takeover)" % (seat["session"], seat["expires_utc"]))
            elif live and steal:
                takeover = "stolen-from-live-holder"
            else:
                takeover = "stale-lease-recovery"
                epoch += 1
        expires = (parse_utc(now) + datetime.timedelta(seconds=int(ttl_s))).strftime("%Y-%m-%dT%H:%M:%SZ")
        new_seat = {"session": session, "holder": session, "acquired_utc": now, "renew_utc": now, "expires_utc": expires, "ttl_s": int(ttl_s), "epoch": epoch, "note": takeover or "claimed"}
        atomic_write(self.seat_path, json.dumps(new_seat, indent=2, sort_keys=True) + "\n")
        # git add + commit seat change if repo exists
        if os.path.isdir(os.path.join(self.home, ".git")):
            try:
                run_git(["add", "seat.json"], cwd=self.home)
                # only commit if changed
                st = run_git(["status", "--porcelain", "seat.json"], cwd=self.home)
                if st.stdout.strip():
                    run_git(["commit", "-m", "seat: %s -> %s (%s)" % (prev_holder, session, takeover or "claim")], cwd=self.home)
            except StoreError:
                pass
        if takeover in ("stale-lease-recovery", "stolen-from-live-holder"):
            # record takeover event
            self._append_event_locked({"revision": self.load_state().get("revision", 0) + 1 if os.path.exists(self.state_path) else 1, "ts": now, "writer": session, "kind": "takeover", "project": None, "data": {"from": prev_holder, "mode": takeover, "epoch": epoch}, "note": takeover})
            # after writing event, update state revision
        return new_seat

    def renew(self, session, ttl_s=None):
        with Lock(self.home):
            seat = self.load_seat()
            if seat.get("session") != session:
                raise StoreError(EXIT_LEASE, "seat held by %r, not %r" % (seat.get("session"), session))
            ttl = int(ttl_s or seat.get("ttl_s") or DEFAULT_TTL_S)
            now = utcnow()
            try:
                if parse_utc(now) >= parse_utc(seat.get("expires_utc") or now):
                    raise StoreError(EXIT_LEASE, "lease already expired; reclaim the seat")
            except StoreError:
                raise
            except Exception:
                pass
            seat["renew_utc"] = now
            seat["expires_utc"] = (parse_utc(now) + datetime.timedelta(seconds=ttl)).strftime("%Y-%m-%dT%H:%M:%SZ")
            atomic_write(self.seat_path, json.dumps(seat, indent=2, sort_keys=True) + "\n")
            if os.path.isdir(os.path.join(self.home, ".git")):
                try:
                    run_git(["add", "seat.json"], cwd=self.home)
                    st = run_git(["status", "--porcelain", "seat.json"], cwd=self.home)
                    if st.stdout.strip():
                        run_git(["commit", "-m", "seat renew %s" % session], cwd=self.home)
                except StoreError:
                    pass
            return seat

    def release(self, session):
        with Lock(self.home):
            seat = self.load_seat()
            if seat.get("session") and seat.get("session") != session:
                raise StoreError(EXIT_LEASE, "seat held by %r, not %r" % (seat.get("session"), session))
            seat.update({"holder": None, "session": None, "expires_utc": None, "note": "released %s" % utcnow()})
            atomic_write(self.seat_path, json.dumps(seat, indent=2, sort_keys=True) + "\n")
            if os.path.isdir(os.path.join(self.home, ".git")):
                try:
                    run_git(["add", "seat.json"], cwd=self.home)
                    st = run_git(["status", "--porcelain", "seat.json"], cwd=self.home)
                    if st.stdout.strip():
                        run_git(["commit", "-m", "seat release %s" % session], cwd=self.home)
                except StoreError:
                    pass
            return seat

    def _append_event_locked(self, event_without_hash):
        """Append event to events.jsonl, update state.json revision, caller holds Lock."""
        events = read_events(self.events_path)
        prev = events[-1]["hash"] if events else ZERO_HASH
        seq = (events[-1]["seq"] + 1) if events else 1
        event = dict(event_without_hash)
        event["seq"] = seq
        event["prev_hash"] = prev
        # revision handling: use provided revision or seq
        if "revision" not in event:
            event["revision"] = seq
        event["hash"] = event_hash(prev, event)
        with open(self.events_path, "a") as f:
            f.write(json.dumps(event, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        # update state.json revision/epoch if exists
        if os.path.exists(self.state_path):
            try:
                state = self.load_state()
                state["revision"] = event["revision"]
                state["updated_utc"] = event["ts"]
                # apply to projects if needed
                self._apply_event_to_state(state, event)
                atomic_write(self.state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")
            except Exception:
                pass
        return event

    @staticmethod
    def _apply_event_to_state(state, event):
        k, p, d = event.get("kind"), event.get("project"), event.get("data") or {}
        if k in ("update", "project-upsert") and p:
            rec = dict(state["projects"].get(p) or {})
            rec.update({"slug": p, "last_writer": event.get("writer"), "updated_utc": event.get("ts")})
            for field in ("status", "summary", "next", "detail_ref", "tags", "blocker", "writer"):
                if field in d:
                    rec[field] = d[field]
            # also carry rev/head_event if present
            if "rev" in d:
                rec["rev"] = d["rev"]
            state["projects"][p] = rec
        elif k == "project-remove" and p:
            state["projects"].pop(p, None)

    # ---- proposal helpers ----
    def list_proposals(self):
        """List proposal branches with manifests."""
        try:
            r = run_git(["for-each-ref", "--format=%(refname)", "refs/heads/orda/prop/"], cwd=self.home)
        except StoreError:
            return []
        refs = [line.strip() for line in r.stdout.splitlines() if line.strip()]
        out = []
        for ref in refs:
            branch = ref.replace("refs/heads/", "")
            manifest_text = git_head_file("proposal.json", cwd=self.home, ref=branch)
            manifest = None
            if manifest_text:
                try:
                    manifest = json.loads(manifest_text)
                except Exception:
                    manifest = {"_raw": manifest_text[:500], "_parse_error": True}
            out.append({"branch": branch, "ref": ref, "manifest": manifest})
        return out

    def list_ingest_proposals(self):
        try:
            r = run_git(["for-each-ref", "--format=%(refname)", "refs/heads/orda/ingest/"], cwd=self.home)
        except StoreError:
            return []
        refs = [line.strip() for line in r.stdout.splitlines() if line.strip()]
        out = []
        for ref in refs:
            branch = ref.replace("refs/heads/", "")
            manifest_text = git_head_file("proposal.json", cwd=self.home, ref=branch)
            manifest = None
            if manifest_text:
                try:
                    manifest = json.loads(manifest_text)
                except Exception:
                    manifest = None
            out.append({"branch": branch, "ref": ref, "manifest": manifest})
        return out

    # ---- verify / reconcile ----
    def verify(self):
        problems = []
        # Read events with corruption handling
        try:
            events = read_events(self.events_path)
        except ValueError as e:
            # Corrupt events.jsonl -> integrity failure (exit 5)
            return {"ok": False, "problems": [str(e)], "events": 0}
        except Exception as e:
            return {"ok": False, "problems": ["events read failed: %s — integrity failure" % e], "events": 0}
        problems.extend(verify_chain(events))
        # Check state.json parses
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path) as f:
                    state = json.load(f)
            except json.JSONDecodeError as e:
                problems.append("state.json corrupt: unparseable JSON (%s) — integrity failure" % e.msg)
                return {"ok": False, "problems": problems, "events": len(events)}
            except Exception as e:
                problems.append("state.json read failed: %s — integrity failure" % e)
                return {"ok": False, "problems": problems, "events": len(events)}
            last_rev = events[-1]["revision"] if events else 0
            if state.get("revision", 0) < last_rev:
                problems.append("state revision %s behind event log revision %s (crash window; run reconcile)" % (state.get("revision"), last_rev))
            if state.get("revision", 0) > last_rev:
                problems.append("state revision %s ahead of event log %s" % (state.get("revision"), last_rev))
            pj = os.path.join(self.home, "projection", "brief.json")
            if os.path.exists(pj):
                try:
                    brief = json.load(open(pj))
                    if brief.get("revision") != state.get("revision"):
                        problems.append("projection brief.json at revision %s, state at %s (stale projection; run reconcile)" % (brief.get("revision"), state.get("revision")))
                except Exception:
                    pass
        # Check each record file parses (detect corrupt git objects via parse)
        rec_dir = os.path.join(self.home, "records")
        if os.path.isdir(rec_dir):
            for fn in os.listdir(rec_dir):
                if fn.endswith(".json"):
                    rp = os.path.join(rec_dir, fn)
                    try:
                        with open(rp) as f:
                            json.load(f)
                    except json.JSONDecodeError as e:
                        problems.append("records/%s corrupt: unparseable JSON (%s) — integrity failure" % (fn, e.msg))
                    except Exception as e:
                        problems.append("records/%s read failed: %s — integrity failure" % (fn, e))
        # git fsck quick — catches corrupt git objects
        try:
            r = run_git(["fsck", "--no-dangling"], cwd=self.home, check=False)
            if r.returncode != 0:
                problems.append("git fsck failed: %s — integrity failure (possible corrupt git object)" % r.stderr.strip()[:500])
        except Exception as e:
            problems.append("git fsck error: %s — integrity failure" % e)
        # J13 store/host boundary: the store is self-contained. If the home
        # sits inside an ancestor git repo (e.g. Eldunari) that repo must not
        # see the store path — otherwise the foreign write cadence leaks into
        # it (missing ignore rule). No home-directory value is embedded here:
        # the ancestor is discovered by walking up from home.
        boundary = check_store_boundary(self.home)
        if boundary:
            problems.append(boundary)
        return {"ok": not problems, "problems": problems, "events": len(events)}

    def reconcile(self, session="reconcile"):
        """Rebuild state.json + projection from events.jsonl + records on main."""
        # First verify integrity — refuse if corrupt (do not silently rebuild)
        report = self.verify()
        # Refuse on any chain integrity failure; only stale state/projection (crash window) is recoverable.
        if not report.get("ok", False):
            non_recoverable = []
            for prob in report.get("problems", []):
                low = prob.lower()
                if "behind" in low or "stale projection" in low:
                    continue
                non_recoverable.append(prob)
            if non_recoverable:
                raise StoreError(EXIT_INTEGRITY, "reconcile refused: chain integrity failure — %s (verify exit 5; repair corruption before reconciling)" % "; ".join(non_recoverable[:2]))
        # If verify passed or only had recoverable staleness, proceed to rebuild
        with Lock(self.home):
            # Re-check events still parse (defensive)
            try:
                events = read_events(self.events_path)
            except ValueError as e:
                raise StoreError(EXIT_INTEGRITY, "reconcile refused: %s" % e)
            # rebuild state from records on disk (main) + events
            state = {"schema_version": SCHEMA_VERSION, "revision": events[-1]["revision"] if events else 0, "epoch": self.load_seat().get("epoch", 1), "updated_utc": events[-1]["ts"] if events else utcnow(), "writer": session, "projects": {}}
            # Load all records from disk
            rec_dir = os.path.join(self.home, "records")
            if os.path.isdir(rec_dir):
                for fn in os.listdir(rec_dir):
                    if fn.endswith(".json"):
                        try:
                            rec = json.load(open(os.path.join(rec_dir, fn)))
                            state["projects"][rec.get("slug") or fn[:-5]] = rec
                        except Exception:
                            # If a record file is corrupt, we already flagged it as corruption above and refused,
                            # so this branch shouldn't be reached for corrupt files.
                            pass
            # ensure revision matches last event
            if events:
                state["revision"] = events[-1]["revision"]
                state["updated_utc"] = events[-1]["ts"]
            atomic_write(self.state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")
            # rebuild projection
            try:
                from orda2.orda2_projection import write_projection
                write_projection(self.home)
            except Exception:
                pass
            return state
