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
EVENTS_DELIVERED = EVENTS_DIR / "delivered"
EVENTS_FAILED = EVENTS_DIR / "failed"

# One writer at a time is plenty for a single-user capture tool and avoids
# torn writes to the JSON files.
_LOCK = threading.RLock()

# Ids we generate/accept are strict slugs — this is the primary guard against
# path traversal in filenames.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_IDEA_ID_RE = re.compile(r"^idea_[a-z0-9]{6,32}$")
_EVENT_ID_RE = re.compile(r"^evt_[a-z0-9]{6,32}$")
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
}

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


def set_reviewer(fn: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]]) -> None:
    """Install (or clear) the review adapter used to process events.

    A reviewer receives a read-only copy of the event-bound idea and returns a
    partial review-result dict (source/classification/intent). It must not touch
    the filesystem or issue side effects beyond returning data.
    """
    global _REVIEWER
    _REVIEWER = fn


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


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _atomic_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")
    tmp.replace(path)


def _ensure_layout() -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    IDEAS_DIR.mkdir(parents=True, exist_ok=True)
    ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    for d in (EVENTS_PENDING, EVENTS_DELIVERED, EVENTS_FAILED):
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


def _normalize_idea(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Build a safe, well-formed idea record from an arbitrary (imported) dict.

    Keeps only known fields, coerces types, validates the id (regenerating it
    if missing/invalid so imports can never write outside the ideas dir), and
    ensures timestamps exist.
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
        "status": raw.get("status") or _DEFAULT_STATUSES[0]["id"],
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
        # v1.1: carry an existing review envelope through import/export untouched
        # (sanitized), or None. Never fabricated for imported ideas.
        "review": _sanitize_review(raw.get("review")),
    }


# --------------------------------------------------------------------------- #
# v1.1 context review — revisions, envelope, outbox, correlation, retrieval
# --------------------------------------------------------------------------- #

# Operator-authored fields that define what a reviewer actually reviews. The
# input revision hashes exactly these, so appending a review, a timeline note,
# or bumping updated_at does NOT invalidate an in-flight event — but any operator
# edit does, which is what makes a stale event detectable.
_REVISION_FIELDS = (
    "title", "summary", "notes_markdown",
    "source_url", "source_type", "category", "subcategory", "tags",
)


def _input_revision(idea: Dict[str, Any]) -> str:
    """Deterministic content hash of the operator-authored idea fields."""
    payload = {}
    for k in _REVISION_FIELDS:
        v = idea.get(k)
        if k == "tags":
            v = sorted(str(t) for t in (v or []))
        payload[k] = v
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

    return {
        "state": _one_of(raw.get("state"), _REVIEW_STATES, "waiting"),
        "input_revision": _s(raw.get("input_revision"), 100) or None,
        "reviewer_version": _s(raw.get("reviewer_version"), 120) or None,
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
            "answer": _s(intent.get("answer"), 4000),
            "answered_at": _s(intent.get("answered_at"), 40) or None,
            "answered_via": _one_of(intent.get("answered_via"), _ANSWER_VIA, None),
        },
        "created_at": _s(raw.get("created_at"), 40) or None,
        "updated_at": _s(raw.get("updated_at"), 40) or None,
        "error_code": _s(raw.get("error_code"), 120) or None,
    }


def _blank_review(input_revision: str) -> Dict[str, Any]:
    now = _now()
    return _sanitize_review({
        "state": "waiting",
        "input_revision": input_revision,
        "reviewer_version": REVIEWER_VERSION,
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


def _pending_tokens() -> Dict[str, str]:
    """Map correlation_token -> idea_id for every idea awaiting an answer."""
    out: Dict[str, str] = {}
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
            out[tok] = data.get("id")
    return out


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
    """Detached HMAC-SHA256 signature for webhook transport authentication."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return "sha256=" + digest


def verify_signature(secret: str, body: bytes, signature: str) -> bool:
    return hmac.compare_digest(sign_payload(secret, body), signature or "")


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
    """An event with this idempotency key that is pending or already delivered.

    Failed events are ignored so a caller can legitimately retry after failure,
    but a live/succeeded event blocks duplicate enqueues and duplicate reviews.
    """
    for d in (EVENTS_PENDING, EVENTS_DELIVERED):
        for ev in _iter_events(d):
            if ev.get("idempotency_key") == key:
                return ev
    return None


def _enqueue_event(idea: Dict[str, Any], event_type: str = "idea.created") -> Optional[Dict[str, Any]]:
    """Write a review event to the outbox. Idempotent per (type, id, revision).

    Returns the event (existing or newly written), or None when context review
    is disabled. Callers treat a None/raise here as non-fatal: capture must
    still succeed.
    """
    if not _review_config().get("enabled"):
        return None
    rev = _input_revision(idea)
    idem = f"{event_type}:{idea['id']}:{rev}"
    existing = _find_event_by_idempotency(idem)
    if existing is not None:
        return existing
    event = {
        "schema_version": 1,
        "event_id": "evt_" + uuid.uuid4().hex[:16],
        "event_type": event_type,
        "idea_id": idea["id"],
        "idea_revision": rev,
        "idempotency_key": idem,
        "created_at": _now(),
        "attempts": 0,
        "last_attempt_at": None,
        "last_error": None,
        "status": "pending",
    }
    _atomic_write(_event_path(EVENTS_PENDING, event["event_id"]), event)
    return event


def _move_event(event: Dict[str, Any], dest: Path) -> None:
    src = _event_path(EVENTS_PENDING, event["event_id"])
    _atomic_write(_event_path(dest, event["event_id"]), event)
    if dest != EVENTS_PENDING and src.exists():
        src.unlink()


def _public_event(ev: Dict[str, Any]) -> Dict[str, Any]:
    """Diagnostics view of an event — no secret material is ever stored here,
    but be explicit about the exposed shape."""
    return {
        "event_id": ev.get("event_id"),
        "event_type": ev.get("event_type"),
        "idea_id": ev.get("idea_id"),
        "idea_revision": ev.get("idea_revision"),
        "status": ev.get("status"),
        "attempts": ev.get("attempts", 0),
        "created_at": ev.get("created_at"),
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
        review = _sanitize_review(review)
        review["updated_at"] = _now()
        idea["review"] = review
        idea["updated_at"] = review["updated_at"]
        if timeline_note:
            idea.setdefault("updates", []).append(
                {"at": review["updated_at"], "by": "reviewer", "body": timeline_note[:10000]}
            )
        _atomic_write(_idea_path(idea_id), idea)
    return idea


def process_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """Run the installed reviewer for one event and persist the result.

    Idempotent and revision-guarded. Never mutates operator-authored fields.
    Retrieval/model failure yields a bounded ``failed`` review state, not a lost
    idea. Returns the (possibly updated) event.
    """
    cfg = _review_config()
    max_attempts = int(cfg.get("max_delivery_attempts", 5) or 5)
    event = dict(event)
    event["attempts"] = int(event.get("attempts", 0)) + 1
    event["last_attempt_at"] = _now()

    idea = _read_json(_idea_path(event["idea_id"]), None)
    if not isinstance(idea, dict):
        event["status"] = "failed"
        event["last_error"] = "idea not found"
        with _LOCK:
            _move_event(event, EVENTS_FAILED)
        return event

    # Stale guard: the idea moved on since the event was enqueued.
    if _input_revision(idea) != event.get("idea_revision"):
        event["status"] = "failed"
        event["last_error"] = "stale event (idea revised)"
        with _LOCK:
            _move_event(event, EVENTS_FAILED)
        return event

    if _REVIEWER is None:
        # Nothing to deliver to yet; leave the event pending for a future drain.
        event["status"] = "pending"
        event["last_error"] = "no reviewer configured"
        with _LOCK:
            _move_event(event, EVENTS_PENDING)
        return event

    try:
        result = _REVIEWER(_idea_for_output(idea)) or {}
    except Exception as exc:  # noqa: BLE001 — bound any adapter failure
        review = _blank_review(event["idea_revision"])
        review["state"] = "failed"
        review["error_code"] = "reviewer_error"
        _apply_review(event["idea_id"], review, "Review failed; idea preserved.")
        if event["attempts"] >= max_attempts:
            event["status"] = "failed"
            event["last_error"] = f"reviewer error: {type(exc).__name__}"
            with _LOCK:
                _move_event(event, EVENTS_FAILED)
        else:
            event["status"] = "pending"
            event["last_error"] = f"reviewer error: {type(exc).__name__}"
            with _LOCK:
                _move_event(event, EVENTS_PENDING)
        return event

    review = _build_review_from_result(idea, event["idea_revision"], result)
    _apply_review(event["idea_id"], review, "Context review completed.")
    event["status"] = "delivered"
    event["last_error"] = None
    with _LOCK:
        _move_event(event, EVENTS_DELIVERED)
    return event


def _build_review_from_result(idea: Dict[str, Any], revision: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Fold a reviewer result into a full envelope, deciding intent + state.

    Source facts, agent suggestions, and operator intent are kept in separate
    sub-objects. A suggested classification is NEVER treated as operator intent.
    """
    review = _blank_review(revision)
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
        # Ask exactly one concise follow-up, correlated by token bound to this
        # revision. The follow-up is only "sent" if delivery is configured;
        # otherwise it waits as needs_context.
        with _LOCK:
            token = _gen_correlation_token(set(_pending_tokens().keys()))
        review["intent"]["status"] = "requested"
        review["intent"]["question"] = str(
            intent.get("question")
            or "What caught your attention: using it directly, borrowing a design "
            "pattern, researching it later, or keeping it as a reference?"
        )[:1000]
        review["intent"]["correlation_token"] = token
        review["intent"]["revision_bound"] = revision
        review["state"] = "needs_context"
    return review


def drain_pending(limit: int = 100) -> List[Dict[str, Any]]:
    """Process up to ``limit`` pending events. Returns the processed events.

    Safe to call repeatedly; each call is idempotent per event because a
    delivered event is moved out of ``pending``.
    """
    processed: List[Dict[str, Any]] = []
    with _LOCK:
        pending = _iter_events(EVENTS_PENDING)[:limit]
    for ev in pending:
        processed.append(process_event(ev))
    return processed


def _prune_events() -> None:
    """Drop delivered/failed events older than the configured retention."""
    days = int(_review_config().get("event_retention_days", 30) or 30)
    if days <= 0:
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    for d in (EVENTS_DELIVERED, EVENTS_FAILED):
        for path in d.glob("evt_*.json"):
            ev = _read_json(path, None)
            if isinstance(ev, dict):
                stamp = ev.get("last_attempt_at") or ev.get("created_at") or ""
                if stamp and stamp < cutoff:
                    path.unlink()


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
        "status": body.status or (_DEFAULT_STATUSES[0]["id"]),
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
            if key == "followup_delivery" and val not in ("none", "preflight", "telegram", "api"):
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
        delivered = [_public_event(e) for e in _iter_events(EVENTS_DELIVERED)]
        failed = [_public_event(e) for e in _iter_events(EVENTS_FAILED)]
    return {
        "enabled": bool(_review_config().get("enabled")),
        "pending": pending,
        "delivered": delivered,
        "failed": failed,
        "counts": {"pending": len(pending), "delivered": len(delivered), "failed": len(failed)},
    }


@router.get("/ideas/{idea_id}/review")
async def get_review(idea_id: str) -> Dict[str, Any]:
    """Return the review envelope for an idea (may be null if never reviewed)."""
    with _LOCK:
        idea = _load_idea(idea_id)
    return {"idea_id": idea_id, "review": _sanitize_review(idea.get("review"))}


@router.post("/ideas/{idea_id}/review-events")
async def enqueue_review(idea_id: str) -> Dict[str, Any]:
    """Enqueue (or re-enqueue) review for the idea's current input revision.

    ``Review again`` binds a fresh event to the current revision. Returns 409
    when context review is disabled — the feature must be enabled first.
    """
    with _LOCK:
        _ensure_layout()
        if not _review_config().get("enabled"):
            raise HTTPException(status_code=409, detail="context review is disabled")
        idea = _load_idea(idea_id)
        event = _enqueue_event(idea, "idea.created")
        # Reset the visible review state to waiting for the current revision,
        # without disturbing any answer already recorded for that revision.
        if event is not None:
            rev = event["idea_revision"]
            existing = idea.get("review") if isinstance(idea.get("review"), dict) else None
            if not (existing and existing.get("input_revision") == rev
                    and (existing.get("intent") or {}).get("status") == "answered"):
                idea["review"] = _blank_review(rev)
                idea["updated_at"] = _now()
                _atomic_write(_idea_path(idea_id), idea)
    return {"idea_id": idea_id, "event": _public_event(event) if event else None,
            "review": _sanitize_review(idea.get("review"))}


@router.post("/ideas/{idea_id}/intent-answer")
async def answer_intent(idea_id: str, body: IntentAnswerIn) -> Dict[str, Any]:
    """Append an operator's intent answer, bound to a valid correlation token.

    Fails closed on expired / used / mismatched / ambiguous tokens. Replaying
    the same answer is idempotent. A reply to an older revision cannot overwrite
    a newer intent answer.
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

        # Server-side resolution: the token must resolve to THIS idea, and this
        # idea must actually be awaiting an answer for it.
        resolved = _pending_tokens().get(token)
        if intent.get("correlation_token") != token:
            raise HTTPException(status_code=409, detail="token does not match this idea")
        if resolved != idea_id and intent.get("status") != "answered":
            raise HTTPException(status_code=409, detail="token not pending for this idea")

        # Idempotent replay: same token + same answer already recorded → no-op.
        if intent.get("status") == "answered":
            if (intent.get("answer") or "") == body.answer.strip():
                return {"idea_id": idea_id, "review": _sanitize_review(review), "idempotent": True}
            raise HTTPException(status_code=409, detail="intent already answered")

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
        # notes, and it is clearly attributed to the operator.
        idea.setdefault("updates", []).append(
            {"at": now, "by": "operator", "body": f"Intent captured ({token}): {intent['answer']}"[:10000]}
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
                }
            else:  # merge by id
                for key in ("categories", "statuses", "templates", "source_types"):
                    existing = cfg.setdefault(key, [])
                    have = {x.get("id") for x in existing}
                    for item in inc.get(key, []) or []:
                        if isinstance(item, dict) and item.get("id") not in have:
                            existing.append(item)
            _save_config(cfg)
            result["config_updated"] = True

        # ---- ideas ----
        if isinstance(body.ideas, list):
            if body.mode == "replace":
                for path in IDEAS_DIR.glob("idea_*.json"):
                    path.unlink()
            for raw in body.ideas:
                if not isinstance(raw, dict):
                    continue
                idea = _normalize_idea(raw)
                _atomic_write(_idea_path(idea["id"]), idea)
                result["ideas_written"] += 1

    return result
