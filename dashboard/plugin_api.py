"""
Preflight — Hermes Idea Capture, dashboard plugin backend.

A lightweight staging area for ideas / future work before they become Kanban
execution items. This is intentionally NOT a project-management system.

Data model (file-backed JSON):

    <DATA_ROOT>/
      categories.json        # category tree + custom status definitions
      ideas/
        idea_<id>.json       # one file per idea item
      attachments/           # reserved for future use

Everything is stored under a single allow-listed data root. All filesystem
access is constrained to that root (see _safe_path); ids are validated so a
malicious id cannot escape the directory via path traversal.

Routes are mounted by Hermes at:  /api/plugins/preflight-idea-capture/
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import socket
import threading
import uuid

try:  # POSIX advisory locking — present on Linux/macOS, absent on Windows.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Storage location (allow-listed, env-overridable)
# --------------------------------------------------------------------------- #

# Data lives under the Hermes home by default; override with the env var.
_HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
_DEFAULT_ROOT = str(_HERMES_HOME / "idea-capture")
DATA_ROOT = Path(os.environ.get("PREFLIGHT_IDEA_CAPTURE_DIR", _DEFAULT_ROOT)).resolve()
IDEAS_DIR = DATA_ROOT / "ideas"
ATTACHMENTS_DIR = DATA_ROOT / "attachments"
CONFIG_FILE = DATA_ROOT / "categories.json"

# v1.1 context-review outbox. Idea persistence stays authoritative; review
# events are enqueued here and drained by an out-of-band sender. Nothing in
# this tree is ever required for capture to succeed.
EVENTS_DIR = DATA_ROOT / "events"
EVENTS_PENDING = EVENTS_DIR / "pending"
EVENTS_PROCESSING = EVENTS_DIR / "processing"  # claimed-but-not-finished lease
EVENTS_DELIVERED = EVENTS_DIR / "delivered"
EVENTS_FAILED = EVENTS_DIR / "failed"
# Artifacts that cannot be safely reconstructed or discarded are moved here for
# an operator to inspect. Never silently deleted — that would be work loss.
EVENTS_QUARANTINE = EVENTS_DIR / "quarantine"
# Cross-process advisory lock file guarding queue transitions. A thread lock
# alone is insufficient: separate worker processes do not share it.
QUEUE_LOCK_FILE = EVENTS_DIR / ".queue.lock"

# One writer at a time is plenty for a single-user capture tool and avoids
# torn writes to the JSON files.
_LOCK = threading.RLock()

# Ids we generate/accept are strict slugs — this is the primary guard against
# path traversal in filenames.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_IDEA_ID_RE = re.compile(r"^idea_[a-z0-9]{6,32}$")
_EVENT_ID_RE = re.compile(r"^evt_[a-z0-9]{6,32}$")
# Suffix for in-flight claim temp files (see _claim_event). Deliberately not
# matched by the evt_*.json glob so a temp file is never mistaken for a lease.
_CLAIM_TMP_SUFFIX = ".claim-tmp"
# Exclusive marker held by whichever reclaimer owns a recovery decision.
_RECLAIM_MARKER_SUFFIX = ".reclaiming"
# Correlation tokens are human-readable and non-authoritative on their own; a
# write always requires server-side resolution in addition to the token. The
# alphabet drops easily-confused characters (0/O, 1/I).
_TOKEN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_TOKEN_RE = re.compile(r"^PF-[A-Z2-9]{4,8}$")

# --------------------------------------------------------------------------- #
# Defaults (seeded on first run, from the project brief)
# --------------------------------------------------------------------------- #

_DEFAULT_STATUSES: List[Dict[str, str]] = [
    {"id": "inbox", "label": "Inbox", "color": "#6b7280"},
    {"id": "parked", "label": "Parked", "color": "#64748b"},
    {"id": "researching", "label": "Researching", "color": "#0ea5e9"},
    {"id": "candidate", "label": "Candidate", "color": "#a855f7"},
    {"id": "promoted", "label": "Promoted", "color": "#22c55e"},
    {"id": "discarded", "label": "Discarded", "color": "#ef4444"},
]

# No categories are seeded — you build your own tree in the Manage view.
_DEFAULT_CATEGORIES: List[Dict[str, Any]] = []

_PRIORITIES = ["", "low", "maybe", "high", "urgent"]

# Where an idea came from. Stored in config so they're user-editable; seeded on
# first run (and lazily for pre-existing installs). "" (unset) is always allowed
# and is prepended by /config as the "—" option — it is not stored here.
_DEFAULT_SOURCE_TYPES: List[Dict[str, str]] = [
    {"id": "reddit", "label": "Reddit", "emoji": "👽"},
    {"id": "twitter", "label": "Twitter / X", "emoji": "🐦"},
    {"id": "hackernews", "label": "Hacker News", "emoji": "📰"},
    {"id": "email", "label": "Email", "emoji": "✉️"},
    {"id": "slack", "label": "Slack", "emoji": "💬"},
    {"id": "client", "label": "Client", "emoji": "💼"},
    {"id": "internal", "label": "Internal", "emoji": "🏠"},
    {"id": "web", "label": "Web", "emoji": "🌐"},
    {"id": "other", "label": "Other", "emoji": "🔖"},
]

# Item templates prefill the new-idea form. Stored in config so they're
# user-editable; seeded on first run (and lazily for pre-existing installs).
# These are generic starters — edit or delete them freely.
_DEFAULT_TEMPLATES: List[Dict[str, Any]] = [
    {
        "id": "quick-idea",
        "name": "Quick idea",
        "source_type": "",
        "status": "inbox",
        "priority": "",
        "tags": [],
        "notes_markdown": "**Idea:** \n\n**Why it matters:** \n\n**Next step:** ",
    },
    {
        "id": "research-note",
        "name": "Research note",
        "source_type": "web",
        "status": "researching",
        "priority": "",
        "tags": ["research"],
        "notes_markdown": "**Question:** \n\n**Findings:** \n\n**Open threads:** ",
    },
    {
        "id": "signal-link",
        "name": "Signal / link",
        "source_type": "reddit",
        "status": "inbox",
        "priority": "maybe",
        "tags": ["signal"],
        "notes_markdown": "**Source:** \n\n**Summary:** \n\n**Why it matters:** ",
    },
]

# v1.1 context review. Defaults OFF: no events are enqueued and no follow-ups
# are attempted until an operator explicitly enables the feature and configures
# a reviewer route. Preflight is fully functional without any of this.
_DEFAULT_CONTEXT_REVIEW: Dict[str, Any] = {
    "enabled": False,
    "grace_period_seconds": 600,
    "max_delivery_attempts": 5,
    "followup_delivery": "none",  # none | preflight | telegram | api
    "digest_window_seconds": 900,
    "event_retention_days": 30,
    # A correlation token is only answerable for this long after it is issued.
    "intent_token_ttl_seconds": 1209600,  # 14 days
    # Bounds for the public-source fetch path (see fetch_public_source).
    "fetch_max_bytes": 1048576,  # 1 MiB
    "fetch_timeout_seconds": 10,
    "fetch_max_redirects": 5,
}
_FOLLOWUP_MODES = {"none", "preflight", "telegram", "api"}

# Allowed enumerations for the review envelope. Anything outside these collapses
# to a safe default rather than being persisted verbatim.
_REVIEW_STATES = {
    "waiting", "reviewing", "reviewed", "needs_context",
    "followup_sent", "answered", "failed", "dismissed",
}
_SOURCE_KINDS = {"article", "repository", "product", "video", "discussion", "research", "other"}
_RETRIEVAL_STATUSES = {"not_needed", "ok", "blocked", "failed"}
_DISPOSITIONS = {
    "keep_reference", "needs_context", "research_later",
    "shape_into_plan", "possible_project", "archive_candidate",
}
_EFFORT_RISK = {"low", "medium", "high", "unknown"}
_INTENT_STATUSES = {"present", "missing", "requested", "answered", "dismissed"}
_ANSWER_VIA = {"preflight", "telegram", "api"}

# The reviewer version stamped onto envelopes this build produces.
REVIEWER_VERSION = "preflight-context-review/1"

# Optional live webhook transport. Both must be set AND context_review.enabled
# must be true before any network delivery is attempted. Absent by default so
# the feature never reaches out on a stock install.
WEBHOOK_URL = os.environ.get("PREFLIGHT_REVIEW_WEBHOOK_URL", "").strip()
WEBHOOK_SECRET = os.environ.get("PREFLIGHT_REVIEW_WEBHOOK_SECRET", "").strip()

# Pluggable reviewer. Left None so a stock process performs no review. Tests and
# adapters install a callable that maps an idea dict to a review-result dict.
# It is never given filesystem, shell, or credential access.
_REVIEWER: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None

# Seams for the two network boundaries. Both default to real stdlib transports
# but are overridable so the full path is exercised offline in tests. Neither is
# ever reached unless context_review is enabled AND a route is configured.
#   _http_post(url, body: bytes, headers: dict) -> (status:int, text:str)
#   _http_open(url) -> (status:int, headers:dict, body:bytes, final_url:str)  (no redirects followed)
_HTTP_POST: Optional[Callable[[str, bytes, Dict[str, str]], Tuple[int, str]]] = None
_HTTP_OPEN: Optional[Callable[[str], Tuple[int, Dict[str, str], bytes, str]]] = None

# Pluggable notification sink for follow-up questions / digests. None means
# "store only" — the follow-up is recorded on the idea but nothing is pushed.
#   _NOTIFIER(kind: str, payload: dict) -> None
_NOTIFIER: Optional[Callable[[str, Dict[str, Any]], None]] = None


def set_reviewer(fn: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]]) -> None:
    """Install (or clear) the review adapter used to process events.

    A reviewer receives a read-only copy of the event-bound idea and returns a
    partial review-result dict (source/classification/intent). It must not touch
    the filesystem or issue side effects beyond returning data.
    """
    global _REVIEWER
    _REVIEWER = fn


def set_http_transport(post=None, opener=None) -> None:
    """Override the HTTP POST (webhook) and single-hop opener (fetch) seams."""
    global _HTTP_POST, _HTTP_OPEN
    _HTTP_POST = post
    _HTTP_OPEN = opener


def set_notifier(fn: Optional[Callable[[str, Dict[str, Any]], None]]) -> None:
    """Install (or clear) the follow-up/digest notification sink."""
    global _NOTIFIER
    _NOTIFIER = fn


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return slug[:64] or "item"


def _safe_path(base: Path, *parts: str) -> Path:
    """Resolve a path and guarantee it stays inside `base`.

    This is defense-in-depth: ids are already slug/idea validated, but we still
    refuse anything that resolves outside the allow-listed root.
    """
    target = base.joinpath(*parts).resolve()
    if base != target and base not in target.parents:
        raise HTTPException(status_code=400, detail="path outside data root")
    return target


def _write_all(fd: int, payload: bytes) -> None:
    """Write every byte of ``payload`` to ``fd`` or raise.

    ``os.write`` may legally write fewer bytes than requested. Ignoring the
    return value lets a short write publish truncated JSON while reporting
    success, so progress is checked on every iteration and any zero/negative
    advance is treated as a write failure rather than silently accepted.
    """
    view = memoryview(payload)
    written = 0
    while written < len(view):
        n = os.write(fd, view[written:])
        if n is None or n <= 0:
            raise OSError(
                f"short write: no progress after {written} of {len(view)} bytes"
            )
        written += n
    if written != len(view):  # defensive; loop guarantees equality
        raise OSError(f"short write: {written} of {len(view)} bytes")


# Cross-process queue serialization. Ordering is always: thread lock first, then
# the file lock, so nesting can never deadlock. A per-thread depth counter makes
# the file lock reentrant alongside the RLock.
_LOCK_DEPTH = threading.local()


class _QueueLock:
    """Reentrant thread + cross-process advisory lock for queue transitions.

    ``_LOCK`` alone only serializes threads inside one interpreter. Independent
    worker processes sharing a data root need a filesystem-level lock, which is
    what the ``flock`` here provides. Falls back to thread-only serialization on
    platforms without ``fcntl``, and says so rather than pretending otherwise.
    """

    def __enter__(self):
        _LOCK.acquire()
        depth = getattr(_LOCK_DEPTH, "n", 0)
        _LOCK_DEPTH.n = depth + 1
        if depth == 0 and fcntl is not None:
            try:
                EVENTS_DIR.mkdir(parents=True, exist_ok=True)
                self._fd = os.open(str(QUEUE_LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o600)
                fcntl.flock(self._fd, fcntl.LOCK_EX)
            except OSError:
                self._fd = None
        else:
            self._fd = None
        return self

    def __exit__(self, *exc):
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
            self._fd = None
        _LOCK_DEPTH.n = getattr(_LOCK_DEPTH, "n", 1) - 1
        _LOCK.release()
        return False


def _queue_lock() -> _QueueLock:
    """Acquire the queue lock (thread + cross-process)."""
    return _QueueLock()


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _atomic_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique temp name so two concurrent writers to the same path can't clobber
    # each other's half-written temp file before the atomic rename.
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            _write_all(fd, payload.encode("utf-8"))
            os.fsync(fd)  # durable bytes only after a complete, checked write
        finally:
            os.close(fd)
        os.replace(str(tmp), str(path))  # atomic on POSIX
        # Persist the directory entry too, so the rename survives a crash.
        try:
            dfd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass  # some platforms disallow directory fsync; rename is still atomic
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _ensure_layout() -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    IDEAS_DIR.mkdir(parents=True, exist_ok=True)
    ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    for d in (EVENTS_PENDING, EVENTS_PROCESSING, EVENTS_DELIVERED, EVENTS_FAILED,
              EVENTS_QUARANTINE):
        d.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        _atomic_write(
            CONFIG_FILE,
            {
                "categories": _clone(_DEFAULT_CATEGORIES),
                "statuses": _clone(_DEFAULT_STATUSES),
                "templates": _clone(_DEFAULT_TEMPLATES),
                "source_types": _clone(_DEFAULT_SOURCE_TYPES),
                "context_review": _clone(_DEFAULT_CONTEXT_REVIEW),
            },
        )


def _clone(obj: Any) -> Any:
    """Deep copy via JSON round-trip (defaults are plain JSON)."""
    return json.loads(json.dumps(obj))


def _load_config() -> Dict[str, Any]:
    cfg = _read_json(CONFIG_FILE, None)
    if not isinstance(cfg, dict):
        cfg = {}
    cfg.setdefault("categories", [])
    cfg.setdefault("statuses", [])
    # A usable status list is an invariant: every idea carries a status, so an
    # empty list would make the API report that no status exists while records
    # still reference one. Reseed the defaults rather than create values the
    # API says do not exist.
    if not isinstance(cfg.get("statuses"), list) or not cfg["statuses"]:
        cfg["statuses"] = _clone(_DEFAULT_STATUSES)
    # Lazily seed fields for installs created before they existed.
    if "templates" not in cfg:
        cfg["templates"] = _clone(_DEFAULT_TEMPLATES)
    if "source_types" not in cfg:
        cfg["source_types"] = _clone(_DEFAULT_SOURCE_TYPES)
    # Lazily seed context_review, filling any missing keys with defaults so old
    # installs and partial configs always expose the full setting surface.
    cr = cfg.get("context_review")
    if not isinstance(cr, dict):
        cr = {}
    merged = _clone(_DEFAULT_CONTEXT_REVIEW)
    merged.update({k: v for k, v in cr.items() if k in _DEFAULT_CONTEXT_REVIEW})
    cfg["context_review"] = merged
    return cfg


def _review_config() -> Dict[str, Any]:
    return _load_config()["context_review"]


def _save_config(cfg: Dict[str, Any]) -> None:
    _atomic_write(CONFIG_FILE, cfg)


def _valid_status(value: Any) -> str:
    """Coerce a status to one that actually exists in the configuration."""
    statuses = _load_config()["statuses"]
    ids = {s.get("id") for s in statuses if isinstance(s, dict)}
    if isinstance(value, str) and value in ids:
        return value
    first = next((s.get("id") for s in statuses if isinstance(s, dict) and s.get("id")), None)
    return first or _DEFAULT_STATUSES[0]["id"]


def _find_category(cfg: Dict[str, Any], cat_id: str) -> Optional[Dict[str, Any]]:
    return next((c for c in cfg["categories"] if c.get("id") == cat_id), None)


def _idea_path(idea_id: str) -> Path:
    if not _IDEA_ID_RE.match(idea_id):
        raise HTTPException(status_code=400, detail="invalid idea id")
    return _safe_path(IDEAS_DIR, f"{idea_id}.json")


def _load_idea(idea_id: str) -> Dict[str, Any]:
    path = _idea_path(idea_id)
    data = _read_json(path, None)
    if not isinstance(data, dict):
        raise HTTPException(status_code=404, detail="idea not found")
    return data


def _clean_url(value: Any) -> str:
    """Return a safe absolute source URL, or blank it.

    Source URLs are rendered as anchors in the dashboard, so only absolute
    http(s) URLs are accepted. Everything else is treated as empty rather
    than being stored or returned to the browser as a clickable href.
    """
    if value is None:
        return ""
    raw = str(value).strip()
    if not raw:
        return ""
    if any(ch.isspace() for ch in raw):
        return ""
    parsed = urlparse(raw)
    if parsed.scheme.lower() not in {"http", "https"}:
        return ""
    if not parsed.netloc:
        return ""
    return raw


def _clean_promoted_to_kanban(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    draft = dict(value)
    draft["source_url"] = _clean_url(draft.get("source_url"))
    return draft


def _idea_for_output(idea: Dict[str, Any]) -> Dict[str, Any]:
    """Return a browser/export-safe copy of an idea without mutating storage."""
    out = dict(idea)
    out["source_url"] = _clean_url(out.get("source_url"))
    out["promoted_to_kanban"] = _clean_promoted_to_kanban(out.get("promoted_to_kanban"))
    if "review" in out:
        out["review"] = _sanitize_review(out.get("review"))
    return out


def _idea_summary(idea: Dict[str, Any]) -> Dict[str, Any]:
    """Trim heavy fields for list responses."""
    review = idea.get("review")
    return {
        "id": idea.get("id"),
        "title": idea.get("title", ""),
        "category": idea.get("category"),
        "subcategory": idea.get("subcategory"),
        "status": idea.get("status"),
        "priority": idea.get("priority", ""),
        "source_url": _clean_url(idea.get("source_url", "")),
        "source_type": idea.get("source_type", ""),
        "summary": idea.get("summary", ""),
        "tags": idea.get("tags", []),
        "update_count": len(idea.get("updates", [])),
        "created_at": idea.get("created_at"),
        "updated_at": idea.get("updated_at"),
        "promoted_to_kanban": _clean_promoted_to_kanban(idea.get("promoted_to_kanban")),
        "archived": bool(idea.get("archived", False)),
        "archived_at": idea.get("archived_at"),
        # Enough for a review-state badge on the list without shipping the whole
        # envelope. None when the idea has never been reviewed.
        "review_state": review.get("state") if isinstance(review, dict) else None,
    }


def _clean_source_type(value: Any) -> str:
    if not value:
        return ""
    ids = {s.get("id") for s in _load_config().get("source_types", [])}
    return value if value in ids else ""


def _normalize_idea(raw: Dict[str, Any], seen_tokens: Optional[set] = None) -> Dict[str, Any]:
    """Build a safe, well-formed idea record from an arbitrary (imported) dict.

    Keeps only known fields, coerces types, validates the id (regenerating it
    if missing/invalid so imports can never write outside the ideas dir), and
    ensures timestamps exist.

    ``seen_tokens`` accumulates every correlation token already reserved (on disk
    and earlier in this import batch) so a duplicate imported token is retired
    rather than becoming a second answerable copy.
    """
    now = _now()
    idea_id = raw.get("id")
    if not (isinstance(idea_id, str) and _IDEA_ID_RE.match(idea_id)):
        idea_id = f"idea_{uuid.uuid4().hex[:12]}"

    updates: List[Dict[str, Any]] = []
    for u in raw.get("updates", []) or []:
        if isinstance(u, dict) and u.get("body"):
            updates.append(
                {
                    "at": str(u.get("at") or now),
                    "by": str(u.get("by") or "me")[:60],
                    "body": str(u.get("body"))[:10000],
                }
            )

    tags = [str(t).strip() for t in (raw.get("tags") or []) if str(t).strip()]
    priority = raw.get("priority", "")
    archived = bool(raw.get("archived", False))
    return {
        "id": idea_id,
        "title": str(raw.get("title") or "(untitled)").strip()[:300],
        "category": raw.get("category") or None,
        "subcategory": raw.get("subcategory") or None,
        "status": _valid_status(raw.get("status")),
        "priority": priority if priority in _PRIORITIES else "",
        "source_url": _clean_url(raw.get("source_url")),
        "source_type": _clean_source_type(raw.get("source_type")),
        "summary": str(raw.get("summary") or "").strip(),
        "notes_markdown": str(raw.get("notes_markdown") or ""),
        "tags": tags,
        "updates": updates,
        "created_at": str(raw.get("created_at") or now),
        "updated_at": str(raw.get("updated_at") or now),
        "promoted_to_kanban": _clean_promoted_to_kanban(raw.get("promoted_to_kanban")),
        "archived": archived,
        "archived_at": str(raw["archived_at"]) if archived and raw.get("archived_at") else None,
        # v1.1: carry an existing review envelope through import/export
        # (sanitized), or None. Never fabricated for imported ideas. Imported
        # follow-up tokens are vetted and fail closed.
        "review": _vet_imported_review(
            _sanitize_review(raw.get("review")),
            seen_tokens if seen_tokens is not None else set(),
        ),
    }


# --------------------------------------------------------------------------- #
# v1.1 context review — revisions, envelope, outbox, correlation, retrieval
# --------------------------------------------------------------------------- #

# Operator-authored fields that define what a reviewer actually reviews. The
# input revision hashes exactly these plus operator-authored timeline updates
# (a note can carry the very context the reviewer needs), so adding real
# operator context changes the revision and makes an in-flight event stale —
# while appending a review or bumping updated_at does not.
_REVISION_FIELDS = (
    "title", "summary", "notes_markdown",
    "source_url", "source_type", "category", "subcategory", "tags",
)
# Timeline authors whose entries count as operator-authored context. System and
# reviewer entries are excluded so machine activity never shifts the revision.
_OPERATOR_UPDATE_AUTHORS = {"me", "operator"}


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp (accepting a trailing Z) or return None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _input_revision(idea: Dict[str, Any]) -> str:
    """Deterministic content hash of the operator-authored idea state."""
    payload = {}
    for k in _REVISION_FIELDS:
        v = idea.get(k)
        if k == "tags":
            v = sorted(str(t) for t in (v or []))
        payload[k] = v
    # Fold in operator-authored update bodies, in order — these can carry the
    # missing "why" a reviewer is meant to pick up.
    payload["operator_updates"] = [
        str(u.get("body") or "")
        for u in (idea.get("updates") or [])
        if isinstance(u, dict) and str(u.get("by") or "") in _OPERATOR_UPDATE_AUTHORS
    ]
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _one_of(value: Any, allowed: set, default: Optional[str]) -> Optional[str]:
    return value if isinstance(value, str) and value in allowed else default


def _sanitize_review(raw: Any) -> Optional[Dict[str, Any]]:
    """Coerce an arbitrary review dict into the bounded envelope shape.

    Drops anything not part of the contract so imported data can never smuggle
    page bodies, secrets, cookies, or tool logs into a persisted idea. Returns
    None when there is no review to keep.
    """
    if not isinstance(raw, dict):
        return None
    src = raw.get("source") if isinstance(raw.get("source"), dict) else {}
    cls = raw.get("classification") if isinstance(raw.get("classification"), dict) else {}
    intent = raw.get("intent") if isinstance(raw.get("intent"), dict) else {}

    def _s(v: Any, limit: int) -> str:
        return str(v or "").strip()[:limit]

    related = [
        r for r in (cls.get("related_idea_ids") or [])
        if isinstance(r, str) and _IDEA_ID_RE.match(r)
    ][:50]

    token = intent.get("correlation_token")
    token = token if isinstance(token, str) and _TOKEN_RE.match(token) else None

    try:
        generation = int(raw.get("generation") or 1)
    except (TypeError, ValueError):
        generation = 1

    return {
        "state": _one_of(raw.get("state"), _REVIEW_STATES, "waiting"),
        "input_revision": _s(raw.get("input_revision"), 100) or None,
        "reviewer_version": _s(raw.get("reviewer_version"), 120) or None,
        # Bumped by each manual "Review again" so a re-review is a distinct
        # event even when the operator content is byte-identical.
        "generation": max(1, min(generation, 10_000)),
        # True while a re-review is in flight over an already-completed review.
        # Cleared automatically when the replacement review lands.
        "review_pending": bool(raw.get("review_pending")),
        "source": {
            "retrieval_status": _one_of(src.get("retrieval_status"), _RETRIEVAL_STATUSES, "not_needed"),
            "title": _s(src.get("title"), 300),
            "kind": _one_of(src.get("kind"), _SOURCE_KINDS, "other"),
            "summary": _s(src.get("summary"), 4000),
        },
        "classification": {
            "suggested_lane": _s(cls.get("suggested_lane"), 120),
            "suggested_category": _s(cls.get("suggested_category"), 120),
            "suggested_subcategory": _s(cls.get("suggested_subcategory"), 120),
            "related_idea_ids": related,
            "potential_value": _s(cls.get("potential_value"), 2000),
            "effort": _one_of(cls.get("effort"), _EFFORT_RISK, "unknown"),
            "risk": _one_of(cls.get("risk"), _EFFORT_RISK, "unknown"),
            "recommended_disposition": _one_of(
                cls.get("recommended_disposition"), _DISPOSITIONS, None
            ),
        },
        "intent": {
            "status": _one_of(intent.get("status"), _INTENT_STATUSES, "missing"),
            "question": _s(intent.get("question"), 1000),
            "correlation_token": token,
            "revision_bound": _s(intent.get("revision_bound"), 100) or None,
            # Issuance/expiry make an unanswered token age out instead of
            # remaining answerable forever (including across an import).
            "token_issued_at": _s(intent.get("token_issued_at"), 40) or None,
            "token_expires_at": _s(intent.get("token_expires_at"), 40) or None,
            "answer": _s(intent.get("answer"), 4000),
            "answered_at": _s(intent.get("answered_at"), 40) or None,
            "answered_via": _one_of(intent.get("answered_via"), _ANSWER_VIA, None),
        },
        "followup": {
            # Notification budget bookkeeping: at most one unanswered follow-up
            # per idea revision/generation.
            "sent_at": _s((raw.get("followup") or {}).get("sent_at"), 40) or None,
            "channel": _one_of((raw.get("followup") or {}).get("channel"), _FOLLOWUP_MODES, None),
            "digest_id": _s((raw.get("followup") or {}).get("digest_id"), 64) or None,
            # A send claim, so two concurrent senders cannot both physically
            # deliver the same follow-up. Held between selection and persist.
            "claim_id": _s((raw.get("followup") or {}).get("claim_id"), 64) or None,
            "claimed_at": _s((raw.get("followup") or {}).get("claimed_at"), 40) or None,
        } if isinstance(raw.get("followup"), dict) else {
            "sent_at": None, "channel": None, "digest_id": None,
            "claim_id": None, "claimed_at": None,
        },
        # Diagnostics for the most recent *attempt*, kept separate from the
        # review result so a failed re-review can be reported without destroying
        # the last completed review the UI is showing.
        "last_attempt": {
            "generation": max(1, min(int((raw.get("last_attempt") or {}).get("generation") or 1), 10_000)),
            "status": _one_of((raw.get("last_attempt") or {}).get("status"),
                              {"failed", "succeeded"}, None),
            "error_code": _s((raw.get("last_attempt") or {}).get("error_code"), 120) or None,
            "at": _s((raw.get("last_attempt") or {}).get("at"), 40) or None,
        } if isinstance(raw.get("last_attempt"), dict) else None,
        "created_at": _s(raw.get("created_at"), 40) or None,
        "updated_at": _s(raw.get("updated_at"), 40) or None,
        "error_code": _s(raw.get("error_code"), 120) or None,
    }


# States that represent a review that actually completed and produced content
# worth preserving across a failed replacement attempt.
_COMPLETED_REVIEW_STATES = {
    "reviewed", "needs_context", "followup_sent", "answered", "dismissed",
}


def _record_attempt_failure(event: Dict[str, Any], idea: Dict[str, Any], error_code: str) -> None:
    """Record that a review attempt failed, preserving any completed review.

    A failed replacement must not destroy the evidence the UI deliberately keeps
    visible during ``review_pending``. So:

    * if a completed review exists, keep it verbatim and record the failure in
      ``last_attempt`` (clearing ``review_pending``, since nothing is in flight);
    * only when there is no completed review to protect does the envelope itself
      become ``failed`` — there is nothing to lose in that case.
    """
    generation = int(event.get("generation", 1))
    now = _now()
    existing = idea.get("review") if isinstance(idea.get("review"), dict) else None
    attempt = {
        "generation": generation,
        "status": "failed",
        "error_code": error_code,
        "at": now,
    }

    if existing and existing.get("state") in _COMPLETED_REVIEW_STATES:
        preserved = _sanitize_review(dict(existing))
        preserved["review_pending"] = False   # the attempt is over
        preserved["last_attempt"] = attempt
        preserved["updated_at"] = now
        try:
            _apply_review(
                event["idea_id"], preserved,
                f"Re-review attempt (generation {generation}) failed; "
                "previous review retained.",
            )
        except HTTPException:
            pass  # a newer review landed meanwhile; leave it alone
        return

    review = _blank_review(event["idea_revision"], generation)
    review["state"] = "failed"
    review["error_code"] = error_code
    review["last_attempt"] = attempt
    try:
        _apply_review(event["idea_id"], review, "Review failed; idea preserved.")
    except HTTPException:
        pass  # a newer review already exists; leave it alone


def _blank_review(input_revision: str, generation: int = 1) -> Dict[str, Any]:
    now = _now()
    return _sanitize_review({
        "state": "waiting",
        "input_revision": input_revision,
        "reviewer_version": REVIEWER_VERSION,
        "generation": generation,
        "intent": {"status": "missing"},
        "created_at": now,
        "updated_at": now,
    })


# ---- correlation tokens ---------------------------------------------------- #


def _gen_correlation_token(taken: set) -> str:
    """A short, human-readable token unique within the retained set."""
    for _ in range(64):
        # uuid4 gives us entropy without Math.random/Date; fold it into the
        # non-ambiguous alphabet.
        n = uuid.uuid4().int
        chars = []
        for _ in range(4):
            n, rem = divmod(n, len(_TOKEN_ALPHABET))
            chars.append(_TOKEN_ALPHABET[rem])
        token = "PF-" + "".join(chars)
        if token not in taken:
            return token
    # Extremely unlikely; widen the token rather than collide.
    return "PF-" + uuid.uuid4().hex[:8].upper()


def _token_index() -> Dict[str, List[str]]:
    """Map correlation_token -> ALL idea ids awaiting an answer for that token.

    Deliberately a list, not a scalar: an import can introduce two pending ideas
    carrying the same token, and collapsing that to one silently would let an
    answer land on an arbitrary idea. Callers must treat len() > 1 as ambiguous
    and fail closed.
    """
    out: Dict[str, List[str]] = {}
    for path in IDEAS_DIR.glob("idea_*.json"):
        data = _read_json(path, None)
        if not isinstance(data, dict):
            continue
        review = data.get("review")
        if not isinstance(review, dict):
            continue
        intent = review.get("intent") or {}
        tok = intent.get("correlation_token")
        if tok and intent.get("status") in ("requested", "missing"):
            out.setdefault(tok, []).append(data.get("id"))
    return out


def _all_known_tokens() -> set:
    """Every token currently recorded on any idea, pending or not.

    Used when minting a new token so we never reuse one that still appears in
    an answered/dismissed record — reuse would make history ambiguous.
    """
    seen = set()
    for path in IDEAS_DIR.glob("idea_*.json"):
        data = _read_json(path, None)
        if not isinstance(data, dict):
            continue
        review = data.get("review")
        if isinstance(review, dict):
            tok = (review.get("intent") or {}).get("correlation_token")
            if tok:
                seen.add(tok)
    return seen


def _token_expired(intent: Dict[str, Any]) -> bool:
    """True when a token must no longer be answerable.

    Fails **closed**: a missing or unparseable expiry counts as expired rather
    than "never expires". Every token this build mints carries a valid expiry, so
    the only way to reach this path without one is imported or hand-edited data —
    exactly the case that must not be trusted.
    """
    raw = intent.get("token_expires_at")
    if not raw:
        return True                      # no expiry → not answerable
    exp = _parse_ts(raw)
    if exp is None:
        return True                      # malformed expiry → not answerable
    return _now_dt() > exp


def _retire_imported_token(review: Dict[str, Any], reason: str) -> None:
    """Make an untrustworthy imported follow-up token unanswerable.

    The question and review content are kept for the operator, but the token
    itself is cleared so no reply can bind to it. A fresh ``Review again`` mints
    a valid, unique token.
    """
    intent = review.get("intent") or {}
    intent["correlation_token"] = None
    intent["status"] = "missing"
    intent["token_issued_at"] = None
    intent["token_expires_at"] = None
    review["intent"] = intent
    review["error_code"] = reason
    if review.get("state") in ("followup_sent", "needs_context"):
        review["state"] = "needs_context"
    followup = review.get("followup") or {}
    followup["sent_at"] = None
    followup["claim_id"] = None
    followup["claimed_at"] = None
    review["followup"] = followup


def _vet_imported_review(review: Optional[Dict[str, Any]], seen_tokens: set) -> Optional[Dict[str, Any]]:
    """Validate an imported review envelope's token, failing closed.

    An imported token stays answerable only if it has a valid issuance AND
    expiry timestamp and is unique across **every** retained token state — not
    merely against currently-pending ideas. Answered history counts, so an import
    cannot resurrect a used token as a new pending one.
    """
    if not isinstance(review, dict):
        return review
    intent = review.get("intent") or {}
    token = intent.get("correlation_token")
    if not token:
        return review

    if intent.get("status") == "requested":
        issued = _parse_ts(intent.get("token_issued_at"))
        expires = _parse_ts(intent.get("token_expires_at"))
        if issued is None or expires is None:
            _retire_imported_token(review, "imported_token_missing_timestamps")
            return review
        # Ordering matters as much as parseability: expiry at or before issuance
        # describes a window that never existed, even if both are in the future.
        if expires <= issued:
            _retire_imported_token(review, "imported_token_invalid_order")
            return review
        if _now_dt() > expires:
            _retire_imported_token(review, "imported_token_expired")
            return review
        if token in seen_tokens:
            _retire_imported_token(review, "imported_token_collision")
            return review

    # Reserve the token against every later import in this batch, whatever its
    # state, so answered history cannot be reused either.
    seen_tokens.add(token)
    return review


# ---- SSRF-safe public source retrieval guard ------------------------------- #


def _ip_is_blocked(ip: str) -> bool:
    """True if an IP is loopback, private, link-local, reserved, or metadata."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    # The cloud metadata service and its IPv6 form are always refused.
    if str(addr) in ("169.254.169.254", "fd00:ec2::254"):
        return True
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
        or getattr(addr, "is_site_local", False)
    )


def validate_public_url(url: str, *, resolve: bool = True) -> Tuple[bool, str]:
    """Vet a URL for public source retrieval. Returns (ok, reason).

    Enforced before every request AND every redirect hop by a caller. Rejects
    non-http(s) schemes and any destination that resolves to a loopback,
    private, link-local, reserved, or metadata address. When ``resolve`` is
    False the DNS step is skipped (literal-IP checks still apply) — used by
    tests to stay offline.
    """
    raw = (url or "").strip()
    if not raw or any(ch.isspace() for ch in raw):
        return False, "empty or malformed url"
    parsed = urlparse(raw)
    if parsed.scheme.lower() not in ("http", "https"):
        return False, "scheme must be http or https"
    host = parsed.hostname
    if not host:
        return False, "missing host"

    # Literal IP in the URL: check it directly, no DNS.
    try:
        ipaddress.ip_address(host)
        return (False, "blocked ip") if _ip_is_blocked(host) else (True, "ok")
    except ValueError:
        pass

    if not resolve:
        return True, "ok (unresolved)"

    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror:
        return False, "dns resolution failed"
    for info in infos:
        ip = info[4][0]
        if _ip_is_blocked(ip):
            return False, f"resolves to blocked address {ip}"
    return True, "ok"


# ---- HMAC webhook signing -------------------------------------------------- #


def sign_payload(secret: str, body: bytes) -> str:
    """Detached HMAC-SHA256 signature for webhook transport authentication.

    An empty secret is a configuration error, not a valid key: signing with it
    would produce a signature anyone could compute. Refuse rather than mint one.
    """
    if not secret:
        raise ValueError("refusing to sign with an empty secret")
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return "sha256=" + digest


def verify_signature(secret: str, body: bytes, signature: str) -> bool:
    """Constant-time signature check. Fails closed on empty secret/signature."""
    if not secret or not signature:
        return False
    try:
        expected = sign_payload(secret, body)
    except ValueError:
        return False
    return hmac.compare_digest(expected, signature)


# ---- bounded public source retrieval --------------------------------------- #
#
# Fetched pages are DATA, never instructions. Nothing retrieved here is treated
# as agent authority, and only a bounded summary plus retrieval metadata is ever
# persisted — never the raw body.

# Only text-ish documents are considered. Binary attachments are out of scope
# for v1.1 and are refused rather than downloaded.
_ALLOWED_CONTENT_PREFIXES = ("text/html", "text/plain", "application/xhtml", "application/json")

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_WS_RE = re.compile(r"\s+")


def _default_http_open(url: str) -> Tuple[int, Dict[str, str], bytes, str]:
    """Single HTTP(S) hop with NO redirect following (the caller re-validates).

    Imported lazily so the module has no import-time network dependency.
    """
    import urllib.request  # noqa: PLC0415 — deliberately lazy

    cfg = _review_config()
    timeout = int(cfg.get("fetch_timeout_seconds", 10) or 10)
    max_bytes = int(cfg.get("fetch_max_bytes", 1048576) or 1048576)

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None  # surface the redirect to the caller instead

    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Preflight-ContextReview/1 (+bounded public fetch)",
                 "Accept": "text/html,text/plain;q=0.9,*/*;q=0.1"},
        method="GET",
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            headers = {k.lower(): v for k, v in resp.headers.items()}
            # Read one byte past the cap so an oversize body is detectable.
            body = resp.read(max_bytes + 1)
            return resp.status, headers, body, resp.url
    except Exception as exc:  # noqa: BLE001
        status = getattr(exc, "code", 0) or 0
        headers = {}
        hdrs = getattr(exc, "headers", None)
        if hdrs:
            headers = {k.lower(): v for k, v in hdrs.items()}
        if status:  # an HTTP error response (incl. 3xx surfaced by _NoRedirect)
            return status, headers, b"", url
        raise


def _extract_text(body: bytes, content_type: str) -> Tuple[str, str]:
    """Return (title, plain_text) from a bounded response body.

    Tags are stripped; script/style contents are dropped entirely. This is a
    deliberately dumb extractor — its output is treated as untrusted text.
    """
    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return "", ""
    title = ""
    if "html" in content_type:
        m = _TITLE_RE.search(text)
        if m:
            title = _WS_RE.sub(" ", _TAG_RE.sub("", m.group(1))).strip()[:300]
        text = _SCRIPT_STYLE_RE.sub(" ", text)
        text = _TAG_RE.sub(" ", text)
    return title, _WS_RE.sub(" ", text).strip()


def fetch_public_source(url: str, *, resolve: bool = True) -> Dict[str, Any]:
    """Fetch a public source under strict bounds. Never raises for a bad source.

    Returns ``{retrieval_status, title, text, final_url, reason}`` where
    ``retrieval_status`` is one of ``ok`` / ``blocked`` / ``failed``. Inability to
    retrieve is an ordinary outcome, not a capture failure.

    Enforced on the initial request AND on every redirect hop:
      - absolute http(s) only;
      - SSRF re-validation of each hop's destination (never trust the first);
      - redirect-count, response-size, and time limits;
      - text-only content types (no binary downloads);
      - only a bounded summary is returned — the raw body is discarded.
    """
    cfg = _review_config()
    max_redirects = int(cfg.get("fetch_max_redirects", 5) or 5)
    max_bytes = int(cfg.get("fetch_max_bytes", 1048576) or 1048576)
    opener = _HTTP_OPEN or _default_http_open

    current = (url or "").strip()
    seen = set()
    for _ in range(max_redirects + 1):
        okay, reason = validate_public_url(current, resolve=resolve)
        if not okay:
            return {"retrieval_status": "blocked", "title": "", "text": "",
                    "final_url": current, "reason": reason}
        if current in seen:
            return {"retrieval_status": "failed", "title": "", "text": "",
                    "final_url": current, "reason": "redirect loop"}
        seen.add(current)

        try:
            status, headers, body, final_url = opener(current)
        except Exception as exc:  # noqa: BLE001 — network failure is ordinary
            return {"retrieval_status": "failed", "title": "", "text": "",
                    "final_url": current, "reason": f"fetch error: {type(exc).__name__}"}

        # Redirect: re-validate the next hop from the top of this loop.
        if status in (301, 302, 303, 307, 308):
            location = headers.get("location", "")
            if not location:
                return {"retrieval_status": "failed", "title": "", "text": "",
                        "final_url": current, "reason": "redirect without location"}
            # Relative locations resolve against the current URL.
            from urllib.parse import urljoin  # noqa: PLC0415
            current = urljoin(current, location)
            continue

        if status != 200:
            return {"retrieval_status": "failed", "title": "", "text": "",
                    "final_url": final_url or current, "reason": f"http {status}"}

        content_type = (headers.get("content-type") or "").split(";")[0].strip().lower()
        if content_type and not content_type.startswith(_ALLOWED_CONTENT_PREFIXES):
            return {"retrieval_status": "blocked", "title": "", "text": "",
                    "final_url": final_url or current,
                    "reason": f"unsupported content type {content_type}"}

        declared = headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            return {"retrieval_status": "blocked", "title": "", "text": "",
                    "final_url": final_url or current, "reason": "content-length over limit"}
        if len(body) > max_bytes:
            return {"retrieval_status": "blocked", "title": "", "text": "",
                    "final_url": final_url or current, "reason": "response over size limit"}

        title, text = _extract_text(body, content_type)
        return {"retrieval_status": "ok", "title": title, "text": text[:20000],
                "final_url": final_url or current, "reason": "ok"}

    return {"retrieval_status": "failed", "title": "", "text": "",
            "final_url": current, "reason": "too many redirects"}


# ---- webhook delivery ------------------------------------------------------ #


def _webhook_target() -> Tuple[str, str]:
    """(url, secret) for live delivery, or ("", "") when not fully configured."""
    if not WEBHOOK_URL or not WEBHOOK_SECRET:
        return "", ""
    return WEBHOOK_URL, WEBHOOK_SECRET


def deliver_event_webhook(event: Dict[str, Any]) -> Tuple[bool, str]:
    """POST one minimal event to the configured reviewer, HMAC-signed.

    Returns (ok, detail). The payload carries no idea body and no fetched source
    content — the reviewer retrieves the canonical record by validated idea id.
    Refuses to send at all unless a URL *and* a non-empty secret are configured
    and the destination passes the same SSRF validation as source retrieval.
    """
    url, secret = _webhook_target()
    if not url:
        return False, "webhook not configured"
    okay, reason = validate_public_url(url, resolve=(_HTTP_POST is None))
    if not okay:
        return False, f"webhook url rejected: {reason}"

    payload = {
        "schema_version": event.get("schema_version", 1),
        "event_id": event.get("event_id"),
        "event_type": event.get("event_type"),
        "idea_id": event.get("idea_id"),
        "idea_revision": event.get("idea_revision"),
        "created_at": event.get("created_at"),
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    try:
        signature = sign_payload(secret, body)
    except ValueError as exc:
        return False, str(exc)
    headers = {
        "Content-Type": "application/json",
        "X-Preflight-Signature": signature,
        "X-Preflight-Event-Id": str(event.get("event_id") or ""),
        # Deterministic key so the receiver can dedupe replays itself.
        "X-Preflight-Idempotency-Key": str(event.get("idempotency_key") or ""),
    }
    poster = _HTTP_POST or _default_http_post
    try:
        status, text = poster(url, body, headers)
    except Exception as exc:  # noqa: BLE001
        return False, f"delivery error: {type(exc).__name__}"
    if 200 <= status < 300:
        return True, "delivered"
    return False, f"http {status}: {(text or '')[:200]}"


def _default_http_post(url: str, body: bytes, headers: Dict[str, str]) -> Tuple[int, str]:
    import urllib.request  # noqa: PLC0415 — deliberately lazy

    timeout = int(_review_config().get("fetch_timeout_seconds", 10) or 10)
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(4096).decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        status = getattr(exc, "code", 0) or 0
        if status:
            return status, ""
        raise


# ---- follow-up notifications ---------------------------------------------- #


def _notify(kind: str, payload: Dict[str, Any]) -> bool:
    """Hand a follow-up/digest to the configured sink. False when store-only."""
    mode = _review_config().get("followup_delivery", "none")
    if mode == "none" or _NOTIFIER is None:
        return False
    try:
        _NOTIFIER(kind, dict(payload))
        return True
    except Exception:  # noqa: BLE001 — a failed notification never breaks review
        return False


def _followup_identity(review: Dict[str, Any]) -> Tuple[Any, Any, Any]:
    """The (revision, generation, token) a follow-up send is bound to."""
    intent = review.get("intent") or {}
    return (
        review.get("input_revision"),
        int(review.get("generation") or 1),
        intent.get("correlation_token"),
    )


def _followup_idempotency_key(idea_id: str, review: Dict[str, Any]) -> str:
    """Stable key for one logical follow-up, so a sink can deduplicate.

    Derived only from the bound identity, so a retry after a crash between
    physical send and acknowledgement reproduces the same key.
    """
    revision, generation, token = _followup_identity(review)
    basis = f"{idea_id}:{revision}:{generation}:{token}"
    return "fu_" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


def send_due_followups(claim_ttl_seconds: int = 300) -> Dict[str, Any]:
    """Deliver pending follow-ups, grouping close-together ones into a digest.

    Notification budget, enforced here:
      - nothing is sent when intent is already present;
      - at most ONE unanswered follow-up per idea revision/generation
        (``followup.sent_at`` is the guard);
      - captures whose follow-ups fall inside ``digest_window_seconds`` of each
        other are grouped into a single digest message instead of N alerts.

    Concurrency protocol — claim, send, compare-and-swap:

    1. **Claim** (locked): each due follow-up gets a durable ``claim_id``
       persisted before the lock is released, so a second concurrent caller sees
       it as claimed and selects nothing. Stale claims expire after
       ``claim_ttl_seconds`` so a crashed sender does not block delivery.
    2. **Send** (unlocked): one physical notification carrying a stable
       ``idempotency_key`` per logical follow-up.
    3. **Commit** (locked): the ``sent_at`` write is applied only if the review
       still has the same revision, generation, token, and ``claim_id``. An
       answer or a newer generation that landed mid-flight therefore wins and is
       never overwritten back to ``followup_sent``.

    Delivery is **at-least-once**, not exactly-once: if the process dies between
    the physical send and the commit, the claim expires and the follow-up is sent
    again. The ``idempotency_key`` is stable across those retries specifically so
    a sink that can deduplicate will collapse them; a sink that cannot may show
    the message twice.
    """
    cfg = _review_config()
    if not cfg.get("enabled") or cfg.get("followup_delivery", "none") == "none":
        return {"sent": 0, "digest": False, "skipped": "delivery disabled"}

    window = int(cfg.get("digest_window_seconds", 900) or 900)
    channel = cfg.get("followup_delivery", "none")
    claim_id = "fc_" + uuid.uuid4().hex[:16]
    now_dt = _now_dt()

    # ---- 1. claim, durably, before releasing the lock -------------------- #
    claimed: List[Dict[str, Any]] = []
    with _queue_lock():
        for path in sorted(IDEAS_DIR.glob("idea_*.json")):
            idea = _read_json(path, None)
            if not isinstance(idea, dict):
                continue
            review = idea.get("review")
            if not isinstance(review, dict):
                continue
            intent = review.get("intent") or {}
            followup = review.get("followup") or {}
            if intent.get("status") != "requested":
                continue          # present / answered / dismissed → no message
            if followup.get("sent_at"):
                continue          # already sent one for this generation
            if _token_expired(intent):
                continue
            held = followup.get("claim_id")
            if held:
                held_at = _parse_ts(followup.get("claimed_at"))
                if held_at is not None and (now_dt - held_at).total_seconds() <= claim_ttl_seconds:
                    continue      # another sender is mid-flight
            followup = dict(followup)
            followup["claim_id"] = claim_id
            followup["claimed_at"] = _now()
            review["followup"] = followup
            idea["review"] = _sanitize_review(review)
            _atomic_write(_idea_path(idea["id"]), idea)
            claimed.append(idea)

    if not claimed:
        return {"sent": 0, "digest": False}

    # Group by review timestamp proximity to decide digest vs single message.
    stamps = [_parse_ts((i.get("review") or {}).get("updated_at")) for i in claimed]
    stamps = [s for s in stamps if s]
    span = (max(stamps) - min(stamps)).total_seconds() if len(stamps) > 1 else 0
    as_digest = len(claimed) > 1 and span <= window
    digest_id = "dg_" + uuid.uuid4().hex[:12] if as_digest else None

    items = [
        {
            "idea_id": i["id"],
            "title": i.get("title", ""),
            "token": ((i.get("review") or {}).get("intent") or {}).get("correlation_token"),
            "question": ((i.get("review") or {}).get("intent") or {}).get("question"),
            "source_title": ((i.get("review") or {}).get("source") or {}).get("title"),
            "idempotency_key": _followup_idempotency_key(i["id"], i.get("review") or {}),
        }
        for i in claimed
    ]

    # ---- 2. one physical send ------------------------------------------- #
    ok = _notify(
        "digest" if as_digest else "followup",
        {
            "items": items,
            "digest_id": digest_id,
            "idempotency_key": digest_id or items[0]["idempotency_key"],
        },
    )

    if not ok:
        # Release the claims so a working sink can retry immediately.
        with _queue_lock():
            for stub in claimed:
                idea = _read_json(_idea_path(stub["id"]), None)
                if not isinstance(idea, dict):
                    continue
                review = idea.get("review")
                if not isinstance(review, dict):
                    continue
                followup = review.get("followup") or {}
                if followup.get("claim_id") != claim_id:
                    continue
                followup["claim_id"] = None
                followup["claimed_at"] = None
                review["followup"] = followup
                idea["review"] = _sanitize_review(review)
                _atomic_write(_idea_path(idea["id"]), idea)
        return {"sent": 0, "digest": as_digest, "skipped": "no sink"}

    # ---- 3. commit only where the bound identity still holds ------------- #
    now = _now()
    committed = 0
    for stub in claimed:
        expected = _followup_identity(stub.get("review") or {})
        with _queue_lock():
            idea = _read_json(_idea_path(stub["id"]), None)
            if not isinstance(idea, dict):
                continue
            review = idea.get("review")
            if not isinstance(review, dict):
                continue
            followup = review.get("followup") or {}
            intent = review.get("intent") or {}
            # CAS: the world must not have moved under us.
            if followup.get("claim_id") != claim_id:
                continue                          # our claim was superseded
            if _followup_identity(review) != expected:
                continue                          # new generation / new token
            if intent.get("status") != "requested":
                continue                          # answered or dismissed mid-flight
            review["followup"] = {
                "sent_at": now, "channel": channel, "digest_id": digest_id,
                "claim_id": None, "claimed_at": None,
            }
            review["state"] = "followup_sent"
            review["updated_at"] = now
            idea["review"] = _sanitize_review(review)
            _atomic_write(_idea_path(idea["id"]), idea)
            committed += 1
    return {
        "sent": committed,
        "claimed": len(claimed),
        "digest": as_digest,
        "digest_id": digest_id,
        "delivery": "at-least-once",
    }


# ---- durable outbox -------------------------------------------------------- #


def _event_path(dirpath: Path, event_id: str) -> Path:
    if not _EVENT_ID_RE.match(event_id):
        raise HTTPException(status_code=400, detail="invalid event id")
    return _safe_path(dirpath, f"{event_id}.json")


def _iter_events(dirpath: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not dirpath.exists():
        return out
    for path in dirpath.glob("evt_*.json"):
        data = _read_json(path, None)
        if isinstance(data, dict):
            out.append(data)
    return out


def _find_event_by_idempotency(key: str) -> Optional[Dict[str, Any]]:
    """An event with this idempotency key that is live or already delivered.

    Failed events are ignored so a caller can legitimately retry after failure,
    but a pending/in-flight/succeeded event blocks duplicate enqueues and
    duplicate reviews.
    """
    for d in (EVENTS_PENDING, EVENTS_PROCESSING, EVENTS_DELIVERED):
        for ev in _iter_events(d):
            if ev.get("idempotency_key") == key:
                return ev
    return None


def _enqueue_event(
    idea: Dict[str, Any],
    event_type: str = "idea.created",
    generation: int = 1,
) -> Optional[Dict[str, Any]]:
    """Write a review event to the outbox.

    Idempotent per (type, idea id, input revision, generation). ``generation`` is
    what lets a deliberate "Review again" enqueue real work for byte-identical
    content, while an accidental double-submit still dedupes.

    Returns the event (existing or newly written), or None when context review
    is disabled. Callers treat a None/raise here as non-fatal: capture must
    still succeed.
    """
    if not _review_config().get("enabled"):
        return None
    rev = _input_revision(idea)
    idem = f"{event_type}:{idea['id']}:{rev}:g{generation}"
    existing = _find_event_by_idempotency(idem)
    if existing is not None:
        return existing
    now = _now()
    grace = int(_review_config().get("grace_period_seconds", 600) or 0)
    event = {
        "schema_version": 1,
        "event_id": "evt_" + uuid.uuid4().hex[:16],
        "event_type": event_type,
        "idea_id": idea["id"],
        "idea_revision": rev,
        "generation": generation,
        "idempotency_key": idem,
        "created_at": now,
        # Reviewing waits out the grace period so a capture the operator is still
        # editing isn't reviewed mid-thought.
        "not_before": (_now_dt() + timedelta(seconds=grace)).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "attempts": 0,
        "last_attempt_at": None,
        "last_error": None,
        "status": "pending",
    }
    _atomic_write(_event_path(EVENTS_PENDING, event["event_id"]), event)
    return event


def _move_event(event: Dict[str, Any], dest: Path, *, owned: bool = False) -> bool:
    """Write the event into ``dest`` and drop it from every other queue dir.

    With ``owned=True`` the transition is refused unless this worker still holds
    the processing lease, so a displaced owner cannot delete or overwrite the
    queue state now belonging to someone else. Recovery paths pass
    ``owned=False`` because taking over is exactly their job.

    Returns True if the transition happened.
    """
    if owned and not _lease_is_mine(event):
        return False
    _atomic_write(_event_path(dest, event["event_id"]), event)
    for d in (EVENTS_PENDING, EVENTS_PROCESSING, EVENTS_DELIVERED, EVENTS_FAILED):
        if d == dest:
            continue
        stale = _event_path(d, event["event_id"])
        if stale.exists():
            try:
                stale.unlink()
            except OSError:
                pass
    return True


def _event_is_due(event: Dict[str, Any]) -> bool:
    """False while the event is still inside its grace period."""
    not_before = _parse_ts(event.get("not_before"))
    return not_before is None or _now_dt() >= not_before


def _claim_event(event_id: str) -> Optional[Dict[str, Any]]:
    """Exclusively claim a pending event for this worker.

    Protocol — the visible claim is published only once it is already complete:

    1. write the full lease (owner + timestamp) to a unique temp file and fsync
       it, so the bytes are durable *before* anything observable happens;
    2. ``os.link()`` that temp file onto the ``processing`` path. ``link()``
       fails with ``EEXIST`` if the destination exists, so it is a no-clobber
       atomic publish and exactly one caller can win;
    3. unlink the temp name and retire the ``pending`` copy.

    Neither ``rename`` nor "``O_EXCL`` create then write" is used. ``rename``
    replaces an existing destination, so it cannot express "claim only if
    unclaimed". Create-then-write publishes an *empty* file first, so a crash or
    write error between the two steps leaves a malformed lease that blocks every
    future claim — the event strands forever. Publishing an already-complete file
    by link makes an incomplete visible lease impossible.

    Crash safety: dying after step 2 leaves a valid lease plus a pending copy.
    The pending copy cannot be re-claimed while the lease exists, and lease
    expiry returns the event to pending. Dying before step 2 leaves only an
    orphan temp file, which recovery sweeps. A crash costs a delay, never a
    double review and never a stranded event.

    Returns the claimed event, or None if another worker owns it.
    """
    src = _event_path(EVENTS_PENDING, event_id)
    dst = _event_path(EVENTS_PROCESSING, event_id)
    EVENTS_PROCESSING.mkdir(parents=True, exist_ok=True)

    ev = _read_json(src, None)
    if not isinstance(ev, dict):
        return None  # already taken, or never there
    ev["status"] = "processing"
    ev["lease_at"] = _now()
    ev["lease_owner"] = "own_" + uuid.uuid4().hex[:16]

    # Step 1: fully-formed, fsynced, and not yet reachable at the claim path.
    # The write is checked byte-for-byte — a short write must fail the claim, not
    # publish truncated JSON while reporting success.
    tmp = dst.with_suffix(f".{uuid.uuid4().hex}{_CLAIM_TMP_SUFFIX}")
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            _write_all(fd, json.dumps(ev, indent=2, ensure_ascii=False).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        # Nothing observable was published, so nothing can be stranded.
        try:
            tmp.unlink()
        except OSError:
            pass
        return None

    # Step 2: no-clobber atomic publish of the complete lease.
    try:
        os.link(str(tmp), str(dst))
    except FileExistsError:
        tmp.unlink()
        return None  # another worker holds the lease
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return None
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass

    # Step 3: we own a complete, durable lease. Retire the pending copy.
    try:
        os.unlink(str(src))
    except FileNotFoundError:
        try:
            os.unlink(str(dst))
        except OSError:
            pass
        return None
    return ev


def _lease_is_mine(event: Dict[str, Any]) -> bool:
    """True if this worker still owns the event's processing lease.

    Consulted before *every* side effect (webhook send, review apply, retry,
    failure write, queue transition), so an owner whose lease was reclaimed or
    taken over cannot act on the event any more.
    """
    owner = event.get("lease_owner")
    if not owner:
        return True  # not lease-tracked (direct process_event call in tests)
    current = _read_json(_event_path(EVENTS_PROCESSING, event["event_id"]), None)
    if not isinstance(current, dict):
        return False
    return current.get("lease_owner") == owner


def _sweep_claim_temps(max_age_seconds: int = 900) -> int:
    """Delete orphaned claim temp files from workers that died before publish.

    These were never reachable at the claim path, so removing them can never
    displace a live owner.
    """
    removed = 0
    if not EVENTS_PROCESSING.exists():
        return 0
    cutoff = _now_dt().timestamp() - max_age_seconds
    for path in EVENTS_PROCESSING.glob(f"*{_CLAIM_TMP_SUFFIX}"):
        try:
            if path.stat().st_mtime <= cutoff:
                path.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def _quarantine_artifact(path: Path, reason: str) -> None:
    """Move an unusable queue artifact aside for inspection instead of deleting.

    Silent deletion of an artifact that cannot be reconstructed is permanent work
    loss, so anything undecidable ends up here with its reason recorded.
    """
    EVENTS_QUARANTINE.mkdir(parents=True, exist_ok=True)
    dest = EVENTS_QUARANTINE / f"{path.stem}.{uuid.uuid4().hex[:8]}.{reason}.json"
    try:
        os.replace(str(path), str(dest))
    except OSError:
        try:
            path.unlink()
        except OSError:
            pass


def _reclaim_stale_leases(max_lease_seconds: int = 900) -> int:
    """Return events abandoned mid-processing to ``pending``, and bound junk.

    Three distinct cases, each failing closed:

    * a **valid live** lease is never touched;
    * a **valid expired** lease is returned to pending and its owner invalidated;
    * a **malformed/incomplete** claim artifact (unparseable, empty, or missing
      its lease stamp) is not a live owner and must not block claims forever, so
      it is quarantined once it is older than the lease window. An unstamped but
      *recent* artifact is left alone, so this can never displace a worker that
      is mid-claim.
    """
    recovered = 0
    _sweep_claim_temps(max_lease_seconds)
    if not EVENTS_PROCESSING.exists():
        return 0

    for path in sorted(EVENTS_PROCESSING.glob("evt_*.json")):
        event_id = path.stem
        # Exclusive takeover marker: only one reclaimer may act on this event,
        # across threads AND processes. Without it, two reclaimers can both
        # inspect one expired lease and the second can delete a lease a brand-new
        # owner has since published.
        marker = EVENTS_PROCESSING / f"{event_id}{_RECLAIM_MARKER_SUFFIX}"
        try:
            mfd = os.open(str(marker), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(mfd)
        except FileExistsError:
            continue          # another reclaimer owns this decision
        except OSError:
            continue

        try:
            observed = _read_json(path, None)
            stamped = _parse_ts(observed.get("lease_at")) if isinstance(observed, dict) else None

            if stamped is not None:
                if (_now_dt() - stamped).total_seconds() <= max_lease_seconds:
                    continue  # valid live lease — never resurrect
                # CAS: prove we are still replacing the exact lease we inspected.
                # If a newer owner republished between our read and now, abort.
                current = _read_json(path, None)
                if not isinstance(current, dict):
                    continue
                if (current.get("lease_owner") != observed.get("lease_owner")
                        or current.get("lease_at") != observed.get("lease_at")):
                    continue  # a newer owner published; leave it alone
                ev = dict(current)
                ev["status"] = "pending"
                ev["last_error"] = "recovered from stale lease"
                ev.pop("lease_owner", None)  # invalidate the displaced owner
                ev.pop("lease_at", None)
                _move_event(ev, EVENTS_PENDING)
                recovered += 1
                continue

            # Malformed or unstamped: only actionable once it is provably not a
            # claim in flight. Age is taken from the filesystem because the
            # content is untrustworthy.
            try:
                age = _now_dt().timestamp() - path.stat().st_mtime
            except OSError:
                continue
            if age <= max(max_lease_seconds, 0):
                continue  # possibly mid-claim; leave it alone

            pending_copy = EVENTS_PENDING / f"{event_id}.json"
            if pending_copy.exists():
                # The authoritative pre-claim copy survived, so the queue entry
                # is not lost. Quarantine the unusable artifact (never a silent
                # delete) and let the pending copy be claimed.
                _quarantine_artifact(path, "malformed_claim_artifact")
                recovered += 1
                continue

            # No authoritative pending copy. Only reconstruct when the salvaged
            # content is sufficient to identify real work; otherwise quarantine
            # so an operator can see it. Deleting here would be permanent loss.
            salvage = dict(observed) if isinstance(observed, dict) else {}
            salvage.setdefault("event_id", event_id)
            salvage["status"] = "pending"
            salvage["last_error"] = "recovered from malformed claim artifact"
            salvage.pop("lease_owner", None)
            salvage.pop("lease_at", None)
            if (_EVENT_ID_RE.match(event_id) and salvage.get("idea_id")
                    and salvage.get("idea_revision")):
                _move_event(salvage, EVENTS_PENDING)
            else:
                _quarantine_artifact(path, "unreconstructable_claim_artifact")
            recovered += 1
        finally:
            try:
                marker.unlink()
            except OSError:
                pass
    return recovered


def _public_event(ev: Dict[str, Any]) -> Dict[str, Any]:
    """Diagnostics view of an event — no secret material is ever stored here,
    but be explicit about the exposed shape."""
    return {
        "event_id": ev.get("event_id"),
        "event_type": ev.get("event_type"),
        "idea_id": ev.get("idea_id"),
        "idea_revision": ev.get("idea_revision"),
        "generation": ev.get("generation", 1),
        "status": ev.get("status"),
        "attempts": ev.get("attempts", 0),
        "created_at": ev.get("created_at"),
        "not_before": ev.get("not_before"),
        "due": _event_is_due(ev),
        "last_attempt_at": ev.get("last_attempt_at"),
        "last_error": ev.get("last_error"),
    }


def _apply_review(idea_id: str, review: Dict[str, Any], timeline_note: Optional[str] = None) -> Dict[str, Any]:
    """Write a review envelope onto an idea WITHOUT touching operator fields.

    Reloads under lock, replaces only ``review`` (and appends an optional
    timeline note), and refuses to clobber a review bound to a newer revision.
    """
    with _LOCK:
        idea = _load_idea(idea_id)
        current = idea.get("review") if isinstance(idea.get("review"), dict) else None
        # Fail closed against stale writes: never let an older input revision
        # overwrite a review already recorded for newer idea bytes.
        if current and current.get("input_revision") and review.get("input_revision"):
            if current["input_revision"] != review["input_revision"]:
                live_rev = _input_revision(idea)
                if review["input_revision"] != live_rev:
                    raise HTTPException(status_code=409, detail="stale review revision")
        # Same revision: only a newer generation may replace the existing review,
        # so a late duplicate of an old generation can't undo a fresh re-review.
        if current and current.get("input_revision") == review.get("input_revision"):
            if int(review.get("generation") or 1) < int(current.get("generation") or 1):
                raise HTTPException(status_code=409, detail="stale review generation")
        review = _sanitize_review(review)
        review["review_pending"] = False  # a landed review clears the in-flight flag
        review["updated_at"] = _now()
        idea["review"] = review
        idea["updated_at"] = review["updated_at"]
        if timeline_note:
            idea.setdefault("updates", []).append(
                {"at": review["updated_at"], "by": "reviewer", "body": timeline_note[:10000]}
            )
        _atomic_write(_idea_path(idea_id), idea)
    return idea


def _abandon(event: Dict[str, Any], reason: str) -> Dict[str, Any]:
    """Stop working an event whose lease we no longer hold, changing nothing."""
    event["status"] = "abandoned"
    event["last_error"] = reason
    return event


def _retry_or_fail(event: Dict[str, Any], error: str, max_attempts: int) -> Dict[str, Any]:
    """Send an event back to pending, or to failed once retries are exhausted.

    Ownership-bound: a worker that lost its lease may not re-queue or fail an
    event that another owner now holds.
    """
    with _queue_lock():
        if not _lease_is_mine(event):
            return _abandon(event, "lease lost before retry/fail")
        event["last_error"] = error
        if int(event.get("attempts", 0)) >= max_attempts:
            event["status"] = "failed"
            _move_event(event, EVENTS_FAILED, owned=True)
        else:
            event["status"] = "pending"
            _move_event(event, EVENTS_PENDING, owned=True)
    return event


def process_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """Run review for one event and persist the result.

    Idempotent, grace-gated, and revision-guarded. Never mutates
    operator-authored fields. Retrieval/model/transport failure yields a bounded
    review state and a bounded retry — never a lost idea. Returns the event.

    Order of work: webhook delivery (if configured) hands the event to an
    external reviewer; an in-process ``_REVIEWER`` is the local alternative. Both
    are optional, and every no-route case is bounded by ``max_delivery_attempts``
    instead of retrying forever.
    """
    cfg = _review_config()
    max_attempts = int(cfg.get("max_delivery_attempts", 5) or 5)
    event = dict(event)

    # Grace period: not yet due → return it to pending untouched (no attempt
    # burned, so a premature drain can't exhaust the retry budget). Still
    # ownership-bound: a displaced owner may not re-queue someone else's event.
    if not _event_is_due(event):
        with _queue_lock():
            if not _lease_is_mine(event):
                return _abandon(event, "lease lost before grace re-queue")
            event["status"] = "pending"
            _move_event(event, EVENTS_PENDING, owned=True)
        return event

    event["attempts"] = int(event.get("attempts", 0)) + 1
    event["last_attempt_at"] = _now()

    idea = _read_json(_idea_path(event["idea_id"]), None)
    if not isinstance(idea, dict):
        with _queue_lock():
            if not _lease_is_mine(event):
                return _abandon(event, "lease lost before missing-idea failure")
            event["status"] = "failed"
            event["last_error"] = "idea not found"
            _move_event(event, EVENTS_FAILED, owned=True)
        return event

    # Stale guard: the idea moved on since the event was enqueued.
    if _input_revision(idea) != event.get("idea_revision"):
        with _queue_lock():
            if not _lease_is_mine(event):
                return _abandon(event, "lease lost before stale-revision failure")
            event["status"] = "failed"
            event["last_error"] = "stale event (idea revised)"
            _move_event(event, EVENTS_FAILED, owned=True)
        return event

    # --- external reviewer via authenticated webhook ---------------------- #
    webhook_url, _ = _webhook_target()
    if webhook_url:
        # Ownership is checked BEFORE the send, not after: a webhook POST is an
        # irreversible external side effect, so a displaced owner must never
        # reach the network at all.
        if not _lease_is_mine(event):
            return _abandon(event, "lease lost before webhook delivery")
        ok, detail = deliver_event_webhook(event)
        if ok:
            # The external reviewer will post results back through the normal
            # review API; the event's job ends at successful hand-off.
            with _queue_lock():
                if not _lease_is_mine(event):
                    return _abandon(event, "lease lost after webhook delivery")
                event["status"] = "delivered"
                event["last_error"] = None
                _move_event(event, EVENTS_DELIVERED, owned=True)
            return event
        if _REVIEWER is None:
            return _retry_or_fail(event, f"webhook: {detail}", max_attempts)
        # Fall through to the local reviewer when one is installed.

    if _REVIEWER is None:
        # No route at all. Bounded, not forever: this counts as an attempt and
        # ends in `failed` once the budget is spent.
        return _retry_or_fail(event, "no reviewer configured", max_attempts)

    # Last check before doing observable work: if our lease was reclaimed, the
    # event now belongs to someone else. Drop it rather than review it twice.
    if not _lease_is_mine(event):
        return _abandon(event, "lease lost before review")

    try:
        result = _REVIEWER(_idea_for_output(idea)) or {}
    except Exception as exc:  # noqa: BLE001 — bound any adapter failure
        # A failure is still a write and a retry, so it is ownership-bound too:
        # a displaced owner may not mark another owner's event failed.
        with _queue_lock():
            if not _lease_is_mine(event):
                return _abandon(event, "lease lost before failure write")
            _record_attempt_failure(event, idea, "reviewer_error")
        return _retry_or_fail(event, f"reviewer error: {type(exc).__name__}", max_attempts)

    review = _build_review_from_result(idea, event["idea_revision"], result,
                                       int(event.get("generation", 1)))
    # Ownership check, apply, and the queue transition happen inside one
    # lock-bound critical section, so no reclaim can interleave between
    # "we still own it" and "the result is committed".
    with _queue_lock():
        if not _lease_is_mine(event):
            return _abandon(event, "lease lost during review")
        try:
            _apply_review(event["idea_id"], review, "Context review completed.")
        except HTTPException as exc:
            detail = exc.detail
            return _retry_or_fail(event, f"apply rejected: {detail}", max_attempts)
        event["status"] = "delivered"
        event["last_error"] = None
        _move_event(event, EVENTS_DELIVERED, owned=True)
    return event


def _build_review_from_result(
    idea: Dict[str, Any],
    revision: str,
    result: Dict[str, Any],
    generation: int = 1,
) -> Dict[str, Any]:
    """Fold a reviewer result into a full envelope, deciding intent + state.

    Source facts, agent suggestions, and operator intent are kept in separate
    sub-objects. A suggested classification is NEVER treated as operator intent.
    """
    review = _blank_review(revision, generation)
    review["source"] = _sanitize_review({"source": result.get("source", {})})["source"]
    review["classification"] = _sanitize_review(
        {"classification": result.get("classification", {})}
    )["classification"]

    intent = result.get("intent") if isinstance(result.get("intent"), dict) else {}
    has_intent = bool(intent.get("present"))
    if has_intent:
        review["intent"]["status"] = "present"
        review["state"] = "reviewed"
    else:
        # Ask exactly one concise follow-up, correlated by a token bound to this
        # revision and generation, with an explicit expiry. The follow-up is only
        # pushed if delivery is configured; otherwise it waits as needs_context.
        ttl = int(_review_config().get("intent_token_ttl_seconds", 1209600) or 1209600)
        issued = _now_dt()
        with _LOCK:
            token = _gen_correlation_token(_all_known_tokens())
        review["intent"]["status"] = "requested"
        review["intent"]["question"] = str(
            intent.get("question")
            or "What caught your attention: using it directly, borrowing a design "
            "pattern, researching it later, or keeping it as a reference?"
        )[:1000]
        review["intent"]["correlation_token"] = token
        review["intent"]["revision_bound"] = revision
        review["intent"]["token_issued_at"] = issued.isoformat(timespec="seconds").replace("+00:00", "Z")
        review["intent"]["token_expires_at"] = (
            (issued + timedelta(seconds=ttl)).isoformat(timespec="seconds").replace("+00:00", "Z")
        )
        review["state"] = "needs_context"
    return review


def drain_pending(limit: int = 100) -> List[Dict[str, Any]]:
    """Claim and process up to ``limit`` due pending events.

    Each event is exclusively claimed into ``processing`` before it is worked
    (see ``_claim_event``), so concurrent drains — in this process or another —
    never double-process one event. Events still inside their grace period are
    left alone. Retention is pruned on the way out.
    """
    _reclaim_stale_leases()
    processed: List[Dict[str, Any]] = []
    with _queue_lock():
        candidates = [
            ev for ev in _iter_events(EVENTS_PENDING) if _event_is_due(ev)
        ][:limit]
    for ev in candidates:
        claimed = _claim_event(ev["event_id"])
        if claimed is None:
            continue  # another worker took it
        processed.append(process_event(claimed))
    _prune_events()
    return processed


def _prune_events() -> None:
    """Drop delivered/failed events older than the configured retention."""
    days = int(_review_config().get("event_retention_days", 30) or 30)
    if days <= 0:
        return
    cutoff = (_now_dt() - timedelta(days=days)).isoformat(timespec="seconds").replace("+00:00", "Z")
    for d in (EVENTS_DELIVERED, EVENTS_FAILED):
        if not d.exists():
            continue
        for path in d.glob("evt_*.json"):
            ev = _read_json(path, None)
            if isinstance(ev, dict):
                stamp = ev.get("last_attempt_at") or ev.get("created_at") or ""
                if stamp and stamp < cutoff:
                    try:
                        path.unlink()
                    except OSError:
                        pass


# --------------------------------------------------------------------------- #
# Pydantic request bodies
# --------------------------------------------------------------------------- #


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class SubcategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class StatusIn(BaseModel):
    label: str = Field(min_length=1, max_length=60)
    color: str = Field(default="#6b7280", max_length=32)


class StatusPatch(BaseModel):
    label: Optional[str] = Field(default=None, max_length=60)
    color: Optional[str] = Field(default=None, max_length=32)


class SourceTypeIn(BaseModel):
    label: str = Field(min_length=1, max_length=60)
    emoji: str = Field(default="", max_length=8)


class SourceTypePatch(BaseModel):
    label: Optional[str] = Field(default=None, max_length=60)
    emoji: Optional[str] = Field(default=None, max_length=8)


class IdeaIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    category: Optional[str] = None
    subcategory: Optional[str] = None
    status: Optional[str] = None
    priority: str = ""
    source_url: str = ""
    source_type: str = ""
    summary: str = ""
    notes_markdown: str = ""
    tags: List[str] = Field(default_factory=list)


class IdeaPatch(BaseModel):
    title: Optional[str] = Field(default=None, max_length=300)
    category: Optional[str] = None
    subcategory: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    source_url: Optional[str] = None
    source_type: Optional[str] = None
    summary: Optional[str] = None
    notes_markdown: Optional[str] = None
    tags: Optional[List[str]] = None


class UpdateIn(BaseModel):
    body: str = Field(min_length=1, max_length=10000)
    by: str = Field(default="me", max_length=60)


class TemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    source_type: str = ""
    status: str = ""
    priority: str = ""
    tags: List[str] = Field(default_factory=list)
    notes_markdown: str = ""


class PromoteIn(BaseModel):
    acceptance_criteria: Optional[List[str]] = None


class ImportIn(BaseModel):
    mode: str = "merge"  # "merge" (default) or "replace"
    config: Optional[Dict[str, Any]] = None
    ideas: Optional[List[Dict[str, Any]]] = None


class IntentAnswerIn(BaseModel):
    token: str = Field(min_length=1, max_length=32)
    answer: str = Field(min_length=1, max_length=4000)
    answered_via: str = "preflight"


class ContextReviewPatch(BaseModel):
    enabled: Optional[bool] = None
    grace_period_seconds: Optional[int] = Field(default=None, ge=0, le=86400)
    max_delivery_attempts: Optional[int] = Field(default=None, ge=1, le=100)
    followup_delivery: Optional[str] = None
    digest_window_seconds: Optional[int] = Field(default=None, ge=0, le=86400)
    event_retention_days: Optional[int] = Field(default=None, ge=0, le=3650)
    intent_token_ttl_seconds: Optional[int] = Field(default=None, ge=60, le=31536000)
    fetch_max_bytes: Optional[int] = Field(default=None, ge=1024, le=33554432)
    fetch_timeout_seconds: Optional[int] = Field(default=None, ge=1, le=120)
    fetch_max_redirects: Optional[int] = Field(default=None, ge=0, le=20)


# --------------------------------------------------------------------------- #
# Router
# --------------------------------------------------------------------------- #

router = APIRouter()


@router.get("/health")
async def health() -> Dict[str, Any]:
    _ensure_layout()
    return {"ok": True, "data_root": str(DATA_ROOT), "writable": os.access(DATA_ROOT, os.W_OK)}


# ---- Config (categories + statuses) --------------------------------------- #


@router.get("/config")
async def get_config() -> Dict[str, Any]:
    with _LOCK:
        _ensure_layout()
        cfg = _load_config()
    return {
        "categories": cfg["categories"],
        "statuses": cfg["statuses"],
        "templates": cfg.get("templates", []),
        "priorities": _PRIORITIES,
        # Prepend the always-available "unset" option; the rest are editable.
        "source_types": [{"id": "", "label": "—", "emoji": ""}] + cfg.get("source_types", []),
        "context_review": cfg.get("context_review", _clone(_DEFAULT_CONTEXT_REVIEW)),
    }


@router.post("/categories")
async def add_category(body: CategoryIn) -> Dict[str, Any]:
    with _LOCK:
        _ensure_layout()
        cfg = _load_config()
        base = _slugify(body.name)
        cat_id, n = base, 2
        existing = {c["id"] for c in cfg["categories"]}
        while cat_id in existing:
            cat_id, n = f"{base}-{n}", n + 1
        category = {"id": cat_id, "name": body.name.strip(), "subcategories": []}
        cfg["categories"].append(category)
        _save_config(cfg)
    return category


@router.patch("/categories/{cat_id}")
async def rename_category(cat_id: str, body: CategoryIn) -> Dict[str, Any]:
    with _LOCK:
        cfg = _load_config()
        cat = _find_category(cfg, cat_id)
        if not cat:
            raise HTTPException(status_code=404, detail="category not found")
        cat["name"] = body.name.strip()
        _save_config(cfg)
    return cat


@router.delete("/categories/{cat_id}")
async def delete_category(cat_id: str) -> Dict[str, Any]:
    with _LOCK:
        cfg = _load_config()
        cat = _find_category(cfg, cat_id)
        if not cat:
            raise HTTPException(status_code=404, detail="category not found")
        cfg["categories"] = [c for c in cfg["categories"] if c["id"] != cat_id]
        _save_config(cfg)
    # Ideas keep their (now-orphaned) category id; they simply show as
    # "Uncategorized" in the UI. We never delete idea content implicitly.
    return {"ok": True}


@router.post("/categories/{cat_id}/subcategories")
async def add_subcategory(cat_id: str, body: SubcategoryIn) -> Dict[str, Any]:
    with _LOCK:
        cfg = _load_config()
        cat = _find_category(cfg, cat_id)
        if not cat:
            raise HTTPException(status_code=404, detail="category not found")
        subs = cat.setdefault("subcategories", [])
        base = _slugify(body.name)
        sub_id, n = base, 2
        existing = {s["id"] for s in subs}
        while sub_id in existing:
            sub_id, n = f"{base}-{n}", n + 1
        sub = {"id": sub_id, "name": body.name.strip()}
        subs.append(sub)
        _save_config(cfg)
    return sub


@router.patch("/categories/{cat_id}/subcategories/{sub_id}")
async def rename_subcategory(cat_id: str, sub_id: str, body: SubcategoryIn) -> Dict[str, Any]:
    with _LOCK:
        cfg = _load_config()
        cat = _find_category(cfg, cat_id)
        if not cat:
            raise HTTPException(status_code=404, detail="category not found")
        sub = next((s for s in cat.get("subcategories", []) if s["id"] == sub_id), None)
        if not sub:
            raise HTTPException(status_code=404, detail="subcategory not found")
        sub["name"] = body.name.strip()
        _save_config(cfg)
    return sub


@router.delete("/categories/{cat_id}/subcategories/{sub_id}")
async def delete_subcategory(cat_id: str, sub_id: str) -> Dict[str, Any]:
    with _LOCK:
        cfg = _load_config()
        cat = _find_category(cfg, cat_id)
        if not cat:
            raise HTTPException(status_code=404, detail="category not found")
        cat["subcategories"] = [s for s in cat.get("subcategories", []) if s["id"] != sub_id]
        _save_config(cfg)
    return {"ok": True}


# ---- Statuses -------------------------------------------------------------- #


@router.post("/statuses")
async def add_status(body: StatusIn) -> Dict[str, Any]:
    with _LOCK:
        cfg = _load_config()
        base = _slugify(body.label)
        st_id, n = base, 2
        existing = {s["id"] for s in cfg["statuses"]}
        while st_id in existing:
            st_id, n = f"{base}-{n}", n + 1
        status = {"id": st_id, "label": body.label.strip(), "color": body.color}
        cfg["statuses"].append(status)
        _save_config(cfg)
    return status


@router.patch("/statuses/{status_id}")
async def edit_status(status_id: str, body: StatusPatch) -> Dict[str, Any]:
    with _LOCK:
        cfg = _load_config()
        status = next((s for s in cfg["statuses"] if s["id"] == status_id), None)
        if not status:
            raise HTTPException(status_code=404, detail="status not found")
        if body.label is not None:
            status["label"] = body.label.strip()
        if body.color is not None:
            status["color"] = body.color
        _save_config(cfg)
    return status


@router.delete("/statuses/{status_id}")
async def delete_status(status_id: str) -> Dict[str, Any]:
    with _LOCK:
        cfg = _load_config()
        cfg["statuses"] = [s for s in cfg["statuses"] if s["id"] != status_id]
        _save_config(cfg)
    return {"ok": True}


# ---- Source types ---------------------------------------------------------- #


@router.post("/source-types")
async def add_source_type(body: SourceTypeIn) -> Dict[str, Any]:
    with _LOCK:
        cfg = _load_config()
        items = cfg.setdefault("source_types", [])
        base = _slugify(body.label)
        st_id, n = base, 2
        existing = {s["id"] for s in items}
        while st_id in existing:
            st_id, n = f"{base}-{n}", n + 1
        item = {"id": st_id, "label": body.label.strip(), "emoji": (body.emoji or "").strip()}
        items.append(item)
        _save_config(cfg)
    return item


@router.patch("/source-types/{source_id}")
async def edit_source_type(source_id: str, body: SourceTypePatch) -> Dict[str, Any]:
    with _LOCK:
        cfg = _load_config()
        item = next((s for s in cfg.get("source_types", []) if s["id"] == source_id), None)
        if not item:
            raise HTTPException(status_code=404, detail="source type not found")
        if body.label is not None:
            item["label"] = body.label.strip()
        if body.emoji is not None:
            item["emoji"] = body.emoji.strip()
        _save_config(cfg)
    return item


@router.delete("/source-types/{source_id}")
async def delete_source_type(source_id: str) -> Dict[str, Any]:
    with _LOCK:
        cfg = _load_config()
        cfg["source_types"] = [s for s in cfg.get("source_types", []) if s["id"] != source_id]
        _save_config(cfg)
    # Ideas keep any now-removed source_type value; it simply shows as its raw id.
    return {"ok": True}


# ---- Ideas ----------------------------------------------------------------- #


@router.get("/ideas")
async def list_ideas(
    category: Optional[str] = None,
    subcategory: Optional[str] = None,
    status: Optional[str] = None,
    source_type: Optional[str] = None,
    q: Optional[str] = None,
    sort: str = "updated",
    archived: str = "false",
) -> Dict[str, Any]:
    with _LOCK:
        _ensure_layout()
        items: List[Dict[str, Any]] = []
        for path in IDEAS_DIR.glob("idea_*.json"):
            data = _read_json(path, None)
            if isinstance(data, dict):
                items.append(data)

    if archived == "true":
        items = [i for i in items if i.get("archived")]
    elif archived != "all":
        items = [i for i in items if not i.get("archived")]
    if category:
        items = [i for i in items if i.get("category") == category]
    if subcategory:
        items = [i for i in items if i.get("subcategory") == subcategory]
    if status:
        items = [i for i in items if i.get("status") == status]
    if source_type:
        items = [i for i in items if i.get("source_type") == source_type]
    if q:
        needle = q.lower()
        items = [
            i
            for i in items
            if needle in (i.get("title", "") or "").lower()
            or needle in (i.get("summary", "") or "").lower()
            or needle in (i.get("notes_markdown", "") or "").lower()
            or any(needle in (t or "").lower() for t in i.get("tags", []))
        ]

    reverse = True
    if sort == "title":
        items.sort(key=lambda i: (i.get("title", "") or "").lower())
        reverse = False
    elif sort == "created":
        items.sort(key=lambda i: i.get("created_at", ""), reverse=True)
    else:  # updated (default)
        items.sort(key=lambda i: i.get("updated_at", ""), reverse=True)

    return {"ideas": [_idea_summary(i) for i in items], "count": len(items)}


@router.get("/ideas/{idea_id}")
async def get_idea(idea_id: str) -> Dict[str, Any]:
    with _LOCK:
        return _idea_for_output(_load_idea(idea_id))


@router.post("/ideas")
async def create_idea(body: IdeaIn) -> Dict[str, Any]:
    now = _now()
    idea = {
        "id": f"idea_{uuid.uuid4().hex[:12]}",
        "title": body.title.strip(),
        "category": body.category,
        "subcategory": body.subcategory,
        "status": _valid_status(body.status),
        "priority": body.priority if body.priority in _PRIORITIES else "",
        "source_url": _clean_url(body.source_url),
        "source_type": _clean_source_type(body.source_type),
        "summary": body.summary.strip(),
        "notes_markdown": body.notes_markdown,
        "tags": [t.strip() for t in body.tags if t and t.strip()],
        "updates": [],
        "created_at": now,
        "updated_at": now,
        "promoted_to_kanban": None,
        "archived": False,
        "archived_at": None,
        "review": None,
    }
    with _LOCK:
        _ensure_layout()
        _atomic_write(_idea_path(idea["id"]), idea)
        # Idea persistence is authoritative and has already completed. Enqueue is
        # best-effort: a failure here must never fail capture.
        try:
            event = _enqueue_event(idea, "idea.created")
            if event is not None:
                idea["review"] = _blank_review(event["idea_revision"])
                _atomic_write(_idea_path(idea["id"]), idea)
        except Exception:  # noqa: BLE001 — capture already succeeded
            pass
    return _idea_for_output(idea)


@router.patch("/ideas/{idea_id}")
async def update_idea(idea_id: str, body: IdeaPatch) -> Dict[str, Any]:
    with _LOCK:
        idea = _load_idea(idea_id)
        changes = body.model_dump(exclude_unset=True)
        prev_status = idea.get("status")

        if "priority" in changes and changes["priority"] not in _PRIORITIES:
            changes["priority"] = idea.get("priority", "")
        # Same invariant as create/import: every mutation path must leave the
        # status present in the exposed configuration.
        if "status" in changes and changes["status"] is not None:
            changes["status"] = _valid_status(changes["status"])
        if "source_type" in changes:
            changes["source_type"] = _clean_source_type(changes["source_type"])
        if "tags" in changes and changes["tags"] is not None:
            changes["tags"] = [t.strip() for t in changes["tags"] if t and t.strip()]
        if "source_url" in changes:
            changes["source_url"] = _clean_url(changes["source_url"])
        for key in ("title", "summary"):
            if key in changes and isinstance(changes[key], str):
                changes[key] = changes[key].strip()

        idea.update({k: v for k, v in changes.items() if v is not None or k in idea})
        idea["updated_at"] = _now()

        # Record a status change as an update entry for the timeline.
        if "status" in changes and changes["status"] and changes["status"] != prev_status:
            idea.setdefault("updates", []).append(
                {
                    "at": idea["updated_at"],
                    "by": "system",
                    "body": f"Status changed: {prev_status or '—'} → {changes['status']}",
                }
            )
        _atomic_write(_idea_path(idea_id), idea)
    return _idea_for_output(idea)


@router.delete("/ideas/{idea_id}")
async def delete_idea(idea_id: str) -> Dict[str, Any]:
    with _LOCK:
        path = _idea_path(idea_id)
        if not path.exists():
            raise HTTPException(status_code=404, detail="idea not found")
        path.unlink()
    return {"ok": True}


@router.post("/ideas/{idea_id}/archive")
async def archive_idea(idea_id: str) -> Dict[str, Any]:
    """Archive an idea: hides it from the default list without deleting it."""
    with _LOCK:
        idea = _load_idea(idea_id)
        if not idea.get("archived"):
            now = _now()
            idea["archived"] = True
            idea["archived_at"] = now
            idea["updated_at"] = now
            idea.setdefault("updates", []).append({"at": now, "by": "system", "body": "Archived"})
            _atomic_write(_idea_path(idea_id), idea)
    return _idea_for_output(idea)


@router.post("/ideas/{idea_id}/unarchive")
async def unarchive_idea(idea_id: str) -> Dict[str, Any]:
    with _LOCK:
        idea = _load_idea(idea_id)
        if idea.get("archived"):
            now = _now()
            idea["archived"] = False
            idea["archived_at"] = None
            idea["updated_at"] = now
            idea.setdefault("updates", []).append({"at": now, "by": "system", "body": "Unarchived"})
            _atomic_write(_idea_path(idea_id), idea)
    return _idea_for_output(idea)


@router.post("/ideas/{idea_id}/updates")
async def add_update(idea_id: str, body: UpdateIn) -> Dict[str, Any]:
    with _LOCK:
        idea = _load_idea(idea_id)
        entry = {"at": _now(), "by": body.by.strip() or "me", "body": body.body.strip()}
        idea.setdefault("updates", []).append(entry)
        idea["updated_at"] = entry["at"]
        _atomic_write(_idea_path(idea_id), idea)
    return _idea_for_output(idea)


# ---- Context review (v1.1) ------------------------------------------------- #
#
# Advisory only. None of these routes mutate operator-authored fields or an
# idea's status / category / priority / archive / promotion state. Review may
# enrich an idea; it may never create work.


@router.patch("/config/context-review")
async def update_context_review(body: ContextReviewPatch) -> Dict[str, Any]:
    """Operator setting for the review feature. Off by default; this is the
    only way it turns on. It grants no reviewer authority over idea content."""
    changes = body.model_dump(exclude_unset=True)
    with _LOCK:
        _ensure_layout()
        cfg = _load_config()
        cr = cfg["context_review"]
        for key, val in changes.items():
            if val is None:
                continue
            if key == "followup_delivery" and val not in _FOLLOWUP_MODES:
                continue
            cr[key] = val
        cfg["context_review"] = cr
        _save_config(cfg)
    return cr


@router.get("/review-events")
async def list_review_events() -> Dict[str, Any]:
    """Operator/diagnostics view of the outbox. No secret material."""
    with _LOCK:
        _ensure_layout()
        pending = [_public_event(e) for e in _iter_events(EVENTS_PENDING)]
        processing = [_public_event(e) for e in _iter_events(EVENTS_PROCESSING)]
        delivered = [_public_event(e) for e in _iter_events(EVENTS_DELIVERED)]
        failed = [_public_event(e) for e in _iter_events(EVENTS_FAILED)]
    return {
        "enabled": bool(_review_config().get("enabled")),
        "webhook_configured": bool(_webhook_target()[0]),
        "pending": pending,
        "processing": processing,
        "delivered": delivered,
        "failed": failed,
        "counts": {
            "pending": len(pending),
            "processing": len(processing),
            "delivered": len(delivered),
            "failed": len(failed),
        },
    }


@router.post("/review-events/drain")
async def drain_review_events(limit: int = 100) -> Dict[str, Any]:
    """Process due events now (grace-gated, lease-claimed) and prune retention.

    Exposed so an operator or a scheduler can drive the outbox; there is no
    hidden background thread. Returns what was actually processed.
    """
    if not _review_config().get("enabled"):
        raise HTTPException(status_code=409, detail="context review is disabled")
    processed = drain_pending(max(1, min(int(limit), 1000)))
    return {"processed": [_public_event(e) for e in processed], "count": len(processed)}


@router.post("/review-events/send-followups")
async def send_followups() -> Dict[str, Any]:
    """Deliver due follow-ups, batching close-together ones into a digest."""
    if not _review_config().get("enabled"):
        raise HTTPException(status_code=409, detail="context review is disabled")
    return send_due_followups()


@router.get("/ideas/{idea_id}/review")
async def get_review(idea_id: str) -> Dict[str, Any]:
    """Return the review envelope for an idea (may be null if never reviewed)."""
    with _LOCK:
        idea = _load_idea(idea_id)
    return {"idea_id": idea_id, "review": _sanitize_review(idea.get("review"))}


@router.post("/ideas/{idea_id}/review-events")
async def enqueue_review(idea_id: str) -> Dict[str, Any]:
    """Enqueue (or re-enqueue) review for the idea's current input revision.

    ``Review again`` always produces real work: it bumps the review *generation*
    so the new event is distinct even when the operator content is byte-identical
    to a previously reviewed revision.

    The last completed review is NOT erased. It stays visible (with
    ``review_pending`` marking that a refresh is in flight) until a replacement
    review actually lands, so a re-review can never leave the idea with less
    information than it had. Returns 409 when context review is disabled.
    """
    with _LOCK:
        _ensure_layout()
        if not _review_config().get("enabled"):
            raise HTTPException(status_code=409, detail="context review is disabled")
        idea = _load_idea(idea_id)
        existing = idea.get("review") if isinstance(idea.get("review"), dict) else None
        next_gen = int((existing or {}).get("generation") or 0) + 1

        event = _enqueue_event(idea, "idea.created", generation=next_gen)
        if event is not None:
            if existing:
                # Preserve the completed review; just flag the in-flight refresh.
                existing["review_pending"] = True
                idea["review"] = _sanitize_review(existing)
            else:
                idea["review"] = _blank_review(event["idea_revision"], next_gen)
            idea["updated_at"] = _now()
            _atomic_write(_idea_path(idea_id), idea)
    return {"idea_id": idea_id, "event": _public_event(event) if event else None,
            "review": idea.get("review")}


@router.post("/ideas/{idea_id}/intent-answer")
async def answer_intent(idea_id: str, body: IntentAnswerIn) -> Dict[str, Any]:
    """Append an operator's intent answer, bound to a valid correlation token.

    Fails closed on malformed, expired, used, mismatched, and ambiguous tokens.
    Replaying the same answer is idempotent. A reply bound to an older revision
    cannot overwrite newer content.
    """
    token = body.token.strip().upper()
    if not _TOKEN_RE.match(token):
        raise HTTPException(status_code=400, detail="malformed correlation token")
    answered_via = body.answered_via if body.answered_via in _ANSWER_VIA else "preflight"

    with _LOCK:
        idea = _load_idea(idea_id)
        review = idea.get("review") if isinstance(idea.get("review"), dict) else None
        if not review:
            raise HTTPException(status_code=404, detail="no review to answer")
        intent = review.get("intent") or {}

        if intent.get("correlation_token") != token:
            raise HTTPException(status_code=409, detail="token does not match this idea")

        # Idempotent replay: same token + same answer already recorded → no-op.
        # Checked before pending-resolution because an answered token is no
        # longer pending by definition.
        if intent.get("status") == "answered":
            if (intent.get("answer") or "") == body.answer.strip():
                return {"idea_id": idea_id, "review": _sanitize_review(review), "idempotent": True}
            raise HTTPException(status_code=409, detail="intent already answered")

        # Server-side resolution with explicit zero/one/many handling. An import
        # can introduce two pending ideas carrying the same token; answering
        # either would be a guess, so refuse rather than pick one.
        matches = _token_index().get(token, [])
        if len(matches) > 1:
            raise HTTPException(status_code=409, detail="ambiguous correlation token")
        if len(matches) == 0:
            raise HTTPException(status_code=409, detail="token not pending")
        if matches[0] != idea_id:
            raise HTTPException(status_code=409, detail="token not pending for this idea")

        # Expiry: an unanswered token ages out rather than staying answerable
        # forever (notably across an export/import round trip).
        if _token_expired(intent):
            raise HTTPException(status_code=409, detail="correlation token expired")

        # A reply must target the revision the token was bound to; if the idea
        # moved on, fail closed rather than attach intent to stale content.
        bound = intent.get("revision_bound") or review.get("input_revision")
        if bound and bound != _input_revision(idea):
            raise HTTPException(status_code=409, detail="token bound to an older revision")

        now = _now()
        intent["status"] = "answered"
        intent["answer"] = body.answer.strip()
        intent["answered_at"] = now
        intent["answered_via"] = answered_via
        review["intent"] = intent
        review["state"] = "answered"
        review["updated_at"] = now
        idea["review"] = _sanitize_review(review)
        idea["updated_at"] = now
        # Recorded as operator-provided intent — it never replaces the original
        # notes. Attributed to "intent" rather than "operator" so that capturing
        # the answer (already authoritative in the envelope) does not itself
        # shift the input revision and invalidate the review that asked for it.
        idea.setdefault("updates", []).append(
            {"at": now, "by": "intent", "body": f"Intent captured ({token}): {intent['answer']}"[:10000]}
        )
        _atomic_write(_idea_path(idea_id), idea)
    return {"idea_id": idea_id, "review": _sanitize_review(idea["review"]), "idempotent": False}


@router.post("/ideas/{idea_id}/review-dismiss")
async def dismiss_review(idea_id: str) -> Dict[str, Any]:
    """Dismiss the current follow-up without changing the idea's disposition."""
    with _LOCK:
        idea = _load_idea(idea_id)
        review = idea.get("review") if isinstance(idea.get("review"), dict) else None
        if not review:
            raise HTTPException(status_code=404, detail="no review to dismiss")
        now = _now()
        intent = review.get("intent") or {}
        intent["status"] = "dismissed"
        # Retire the token so it can never be answered after dismissal.
        intent["correlation_token"] = None
        review["intent"] = intent
        review["state"] = "dismissed"
        review["updated_at"] = now
        idea["review"] = _sanitize_review(review)
        idea["updated_at"] = now
        idea.setdefault("updates", []).append(
            {"at": now, "by": "operator", "body": "Review follow-up dismissed"}
        )
        _atomic_write(_idea_path(idea_id), idea)
    return {"idea_id": idea_id, "review": _sanitize_review(idea["review"])}


# ---- Promote to Kanban (draft only) --------------------------------------- #


def _draft_markdown(idea: Dict[str, Any], criteria: List[str]) -> str:
    lines = ["# " + (idea.get("title") or "(untitled)"), ""]
    if idea.get("summary"):
        lines += [idea["summary"], ""]
    source_url = _clean_url(idea.get("source_url"))
    if source_url:
        lines += ["**Source:** " + source_url, ""]
    lines += ["## Acceptance criteria"]
    lines += ["- [ ] " + c for c in criteria]
    lines += ["", "_Idea ref: " + idea.get("id", "") + "_"]
    return "\n".join(lines)


@router.post("/ideas/{idea_id}/promote-draft")
async def promote_draft(idea_id: str, body: Optional[PromoteIn] = None) -> Dict[str, Any]:
    """Generate a Kanban card *draft* for an idea.

    The body is optional: POST with no body, an empty ``{}``, or
    ``{"acceptance_criteria": [...]}`` all work.

    This is deliberately non-automatic: it only builds a draft, records that a
    draft was generated, and returns it for the user to copy. It does NOT create
    a Kanban card or dispatch anything to any external system.
    """
    with _LOCK:
        idea = _load_idea(idea_id)
        raw_criteria = (body.acceptance_criteria if body else None) or []
        criteria = [c.strip() for c in raw_criteria if c and c.strip()]
        if not criteria:
            criteria = [
                "Define the concrete outcome / deliverable",
                "List what “done” looks like",
            ]
        now = _now()
        draft = {
            "drafted_at": now,
            "title": idea.get("title", ""),
            "summary": idea.get("summary", ""),
            "source_url": _clean_url(idea.get("source_url")),
            "acceptance_criteria": criteria,
            "idea_ref": idea_id,
            "markdown": _draft_markdown(idea, criteria),
        }
        idea["promoted_to_kanban"] = draft
        idea["updated_at"] = now
        idea.setdefault("updates", []).append(
            {"at": now, "by": "system", "body": "Kanban card draft generated (not dispatched)"}
        )
        _atomic_write(_idea_path(idea_id), idea)
    return {"idea": _idea_for_output(idea), "draft": draft}


# ---- Templates ------------------------------------------------------------- #


@router.post("/templates")
async def add_template(body: TemplateIn) -> Dict[str, Any]:
    with _LOCK:
        cfg = _load_config()
        templates = cfg.setdefault("templates", [])
        base = _slugify(body.name)
        tid, n = base, 2
        existing = {t["id"] for t in templates}
        while tid in existing:
            tid, n = f"{base}-{n}", n + 1
        template = {
            "id": tid,
            "name": body.name.strip(),
            "source_type": _clean_source_type(body.source_type),
            "status": body.status,
            "priority": body.priority if body.priority in _PRIORITIES else "",
            "tags": [t.strip() for t in body.tags if t and t.strip()],
            "notes_markdown": body.notes_markdown,
        }
        templates.append(template)
        _save_config(cfg)
    return template


@router.patch("/templates/{template_id}")
async def edit_template(template_id: str, body: TemplateIn) -> Dict[str, Any]:
    with _LOCK:
        cfg = _load_config()
        template = next((t for t in cfg.get("templates", []) if t["id"] == template_id), None)
        if not template:
            raise HTTPException(status_code=404, detail="template not found")
        template.update(
            {
                "name": body.name.strip(),
                "source_type": _clean_source_type(body.source_type),
                "status": body.status,
                "priority": body.priority if body.priority in _PRIORITIES else "",
                "tags": [t.strip() for t in body.tags if t and t.strip()],
                "notes_markdown": body.notes_markdown,
            }
        )
        _save_config(cfg)
    return template


@router.delete("/templates/{template_id}")
async def delete_template(template_id: str) -> Dict[str, Any]:
    with _LOCK:
        cfg = _load_config()
        cfg["templates"] = [t for t in cfg.get("templates", []) if t["id"] != template_id]
        _save_config(cfg)
    return {"ok": True}


# ---- Export / Import ------------------------------------------------------- #


@router.get("/export")
async def export_all() -> Dict[str, Any]:
    with _LOCK:
        _ensure_layout()
        cfg = _load_config()
        ideas: List[Dict[str, Any]] = []
        for path in sorted(IDEAS_DIR.glob("idea_*.json")):
            data = _read_json(path, None)
            if isinstance(data, dict):
                ideas.append(_idea_for_output(data))
    return {"version": 1, "exported_at": _now(), "config": cfg, "ideas": ideas}


@router.post("/import")
async def import_all(body: ImportIn) -> Dict[str, Any]:
    """Import a bundle produced by /export.

    mode="merge" (default) adds/overwrites by id and keeps everything else.
    mode="replace" clears existing ideas (and replaces config) first.
    All idea writes go through _normalize_idea + _idea_path, so imported data
    can never escape the data root.
    """
    if body.mode not in ("merge", "replace"):
        raise HTTPException(status_code=400, detail="mode must be 'merge' or 'replace'")

    result = {"mode": body.mode, "ideas_written": 0, "config_updated": False}
    with _LOCK:
        _ensure_layout()

        # ---- config ----
        if isinstance(body.config, dict):
            cfg = _load_config()
            inc = body.config
            if body.mode == "replace":
                cfg = {
                    "categories": inc.get("categories", []),
                    "statuses": inc.get("statuses", []),
                    "templates": inc.get("templates", cfg.get("templates", [])),
                    "source_types": inc.get("source_types", cfg.get("source_types", _clone(_DEFAULT_SOURCE_TYPES))),
                    # Settings are part of the bundle: a replace-import must not
                    # silently drop the operator's review configuration.
                    "context_review": inc.get("context_review", cfg.get("context_review", _clone(_DEFAULT_CONTEXT_REVIEW))),
                }
            else:  # merge by id
                for key in ("categories", "statuses", "templates", "source_types"):
                    existing = cfg.setdefault(key, [])
                    have = {x.get("id") for x in existing}
                    for item in inc.get(key, []) or []:
                        if isinstance(item, dict) and item.get("id") not in have:
                            existing.append(item)
                # context_review is a settings object, not an id-keyed list:
                # merge its known keys over the current values.
                if isinstance(inc.get("context_review"), dict):
                    cr = cfg.setdefault("context_review", _clone(_DEFAULT_CONTEXT_REVIEW))
                    cr.update({
                        k: v for k, v in inc["context_review"].items()
                        if k in _DEFAULT_CONTEXT_REVIEW
                    })
            _save_config(cfg)
            result["config_updated"] = True

        # ---- ideas ----
        if isinstance(body.ideas, list):
            if body.mode == "replace":
                for path in IDEAS_DIR.glob("idea_*.json"):
                    path.unlink()
            # Reserve every token that survives this import (merge keeps the
            # existing corpus) so an imported duplicate is retired, not made a
            # second answerable copy. Includes answered/dismissed history.
            seen_tokens = _all_known_tokens()
            retired = 0
            for raw in body.ideas:
                if not isinstance(raw, dict):
                    continue
                idea = _normalize_idea(raw, seen_tokens)
                review = idea.get("review")
                if isinstance(review, dict) and str(review.get("error_code") or "").startswith("imported_token"):
                    retired += 1
                _atomic_write(_idea_path(idea["id"]), idea)
                result["ideas_written"] += 1
            if retired:
                result["tokens_retired"] = retired

    return result
