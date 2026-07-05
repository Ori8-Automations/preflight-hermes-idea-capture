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

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

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

# One writer at a time is plenty for a single-user capture tool and avoids
# torn writes to the JSON files.
_LOCK = threading.RLock()

# Ids we generate/accept are strict slugs — this is the primary guard against
# path traversal in filenames.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_IDEA_ID_RE = re.compile(r"^idea_[a-z0-9]{6,32}$")

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

# Where an idea came from. Fixed list (served in /config); "" = unset.
SOURCE_TYPES: List[Dict[str, str]] = [
    {"id": "", "label": "—"},
    {"id": "reddit", "label": "Reddit"},
    {"id": "twitter", "label": "Twitter / X"},
    {"id": "hackernews", "label": "Hacker News"},
    {"id": "email", "label": "Email"},
    {"id": "slack", "label": "Slack"},
    {"id": "client", "label": "Client"},
    {"id": "internal", "label": "Internal"},
    {"id": "web", "label": "Web"},
    {"id": "other", "label": "Other"},
]
_SOURCE_IDS = {s["id"] for s in SOURCE_TYPES}

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
    if not CONFIG_FILE.exists():
        _atomic_write(
            CONFIG_FILE,
            {
                "categories": _clone(_DEFAULT_CATEGORIES),
                "statuses": _clone(_DEFAULT_STATUSES),
                "templates": _clone(_DEFAULT_TEMPLATES),
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
    # Lazily seed templates for installs created before templates existed.
    if "templates" not in cfg:
        cfg["templates"] = _clone(_DEFAULT_TEMPLATES)
    return cfg


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


def _idea_summary(idea: Dict[str, Any]) -> Dict[str, Any]:
    """Trim heavy fields for list responses."""
    return {
        "id": idea.get("id"),
        "title": idea.get("title", ""),
        "category": idea.get("category"),
        "subcategory": idea.get("subcategory"),
        "status": idea.get("status"),
        "priority": idea.get("priority", ""),
        "source_url": idea.get("source_url", ""),
        "source_type": idea.get("source_type", ""),
        "summary": idea.get("summary", ""),
        "tags": idea.get("tags", []),
        "update_count": len(idea.get("updates", [])),
        "created_at": idea.get("created_at"),
        "updated_at": idea.get("updated_at"),
        "promoted_to_kanban": idea.get("promoted_to_kanban"),
    }


def _clean_source_type(value: Any) -> str:
    return value if value in _SOURCE_IDS else ""


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
    return {
        "id": idea_id,
        "title": str(raw.get("title") or "(untitled)").strip()[:300],
        "category": raw.get("category") or None,
        "subcategory": raw.get("subcategory") or None,
        "status": raw.get("status") or _DEFAULT_STATUSES[0]["id"],
        "priority": priority if priority in _PRIORITIES else "",
        "source_url": str(raw.get("source_url") or "").strip(),
        "source_type": _clean_source_type(raw.get("source_type")),
        "summary": str(raw.get("summary") or "").strip(),
        "notes_markdown": str(raw.get("notes_markdown") or ""),
        "tags": tags,
        "updates": updates,
        "created_at": str(raw.get("created_at") or now),
        "updated_at": str(raw.get("updated_at") or now),
        "promoted_to_kanban": raw.get("promoted_to_kanban"),
    }


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
        "source_types": SOURCE_TYPES,
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


# ---- Ideas ----------------------------------------------------------------- #


@router.get("/ideas")
async def list_ideas(
    category: Optional[str] = None,
    subcategory: Optional[str] = None,
    status: Optional[str] = None,
    source_type: Optional[str] = None,
    q: Optional[str] = None,
    sort: str = "updated",
) -> Dict[str, Any]:
    with _LOCK:
        _ensure_layout()
        items: List[Dict[str, Any]] = []
        for path in IDEAS_DIR.glob("idea_*.json"):
            data = _read_json(path, None)
            if isinstance(data, dict):
                items.append(data)

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
        return _load_idea(idea_id)


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
        "source_url": body.source_url.strip(),
        "source_type": _clean_source_type(body.source_type),
        "summary": body.summary.strip(),
        "notes_markdown": body.notes_markdown,
        "tags": [t.strip() for t in body.tags if t and t.strip()],
        "updates": [],
        "created_at": now,
        "updated_at": now,
        "promoted_to_kanban": None,
    }
    with _LOCK:
        _ensure_layout()
        _atomic_write(_idea_path(idea["id"]), idea)
    return idea


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
        for key in ("title", "source_url", "summary"):
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
    return idea


@router.delete("/ideas/{idea_id}")
async def delete_idea(idea_id: str) -> Dict[str, Any]:
    with _LOCK:
        path = _idea_path(idea_id)
        if not path.exists():
            raise HTTPException(status_code=404, detail="idea not found")
        path.unlink()
    return {"ok": True}


@router.post("/ideas/{idea_id}/updates")
async def add_update(idea_id: str, body: UpdateIn) -> Dict[str, Any]:
    with _LOCK:
        idea = _load_idea(idea_id)
        entry = {"at": _now(), "by": body.by.strip() or "me", "body": body.body.strip()}
        idea.setdefault("updates", []).append(entry)
        idea["updated_at"] = entry["at"]
        _atomic_write(_idea_path(idea_id), idea)
    return idea


# ---- Promote to Kanban (draft only) --------------------------------------- #


def _draft_markdown(idea: Dict[str, Any], criteria: List[str]) -> str:
    lines = ["# " + (idea.get("title") or "(untitled)"), ""]
    if idea.get("summary"):
        lines += [idea["summary"], ""]
    if idea.get("source_url"):
        lines += ["**Source:** " + idea["source_url"], ""]
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
            "source_url": idea.get("source_url", ""),
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
    return {"idea": idea, "draft": draft}


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
                ideas.append(data)
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
                }
            else:  # merge by id
                for key in ("categories", "statuses", "templates"):
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
