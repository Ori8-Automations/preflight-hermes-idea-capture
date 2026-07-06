# Preflight — Hermes Idea Capture

Preflight is a lightweight **idea-capture** dashboard plugin for the
[Hermes Agent](https://hermes-agent.nousresearch.com). It gives operators a
simple place to collect ideas, market signals, future projects, and rough notes
before promoting them into executable agent work. It is intentionally **not** a
full project-management system.

```
Category  →  Subcategory  →  Idea  →  notes · status · source · updates
```

It's the front of a simple pipeline:

```
signal / thought / maybe
  → structured idea
  → candidate work
  → human-approved Kanban action
  → agent execution
```

Preflight owns the first two steps and stops there — promoting to Kanban is an
explicit, human action (and even then it only drafts a card for you to copy).

<!-- Add a screenshot here once you've deployed it, e.g.:
![Preflight](docs/screenshot.png)
-->

## Features

- **Dashboard tab** in the Hermes Dashboard.
- **⚡ Quick capture** — a friction-free single-line bar that stays open so you
  can dump several ideas in a row.
- **Ideas** with title, category/subcategory, status, priority, source URL,
  **source type**, tags, and markdown notes.
- **Item templates** — start a new idea from a template that prefills status,
  priority, source type, tags, and a notes scaffold.
- **Sidebar tree** of categories → subcategories with live counts.
- **Filter** by category/subcategory/status/source type, free-text search, and
  sort. All filtering is client-side and instant.
- **Detail pane** with a live markdown preview and an append-only update
  timeline. Status changes are auto-logged.
- **Draft a Kanban card** — generate a copy-ready card draft (title, summary,
  source, acceptance-criteria checklist, and an idea back-reference). It's a
  draft only; nothing is created or dispatched automatically.
- **Custom statuses** (with colors), **custom source types**, and
  **export/import** of the whole dataset as a single JSON file — all managed
  from the **Manage** view.
- **Readable markdown tables** — GitHub-style pipe tables render as scrollable
  tables in notes preview, with higher-contrast inline code pills for dark mode.
- **Dark-mode first**, with a light fallback.
- **Mobile-friendly responsive layout** with stacked controls, horizontal category chips, full-width cards, and a full-screen detail editor on phone-sized screens.

No dummy data: Preflight starts with an empty category tree, a set of sensible
default statuses, a few generic starter templates, and a generic list of source
types — all editable.

## Requirements

- A running Hermes Agent dashboard (FastAPI backend).
- No build step and no npm dependencies — the frontend renders via the Hermes
  Plugin SDK. No external Python dependencies beyond what Hermes already ships
  (FastAPI + Pydantic).

## Install

Clone this repository into your Hermes plugins path, or copy the repository
directory there, then enable it and (re)start the dashboard:

```bash
git clone https://github.com/ori8automations/preflight-hermes-idea-capture.git ~/.hermes/plugins/preflight-idea-capture
hermes plugins enable preflight-idea-capture
hermes dashboard --host 127.0.0.1 --port 9119 --no-open
```

**Manual config fallback** — if you manage plugins via config instead of the
CLI, add the plugin to your Hermes `config.yaml`:

```yaml
plugins:
  enabled:
    - preflight-idea-capture
```

then restart the dashboard (or, if it's already running, force a rescan):

```bash
curl http://127.0.0.1:9119/api/dashboard/plugins/rescan
```

The **Preflight** tab appears in the dashboard nav.

> Repo layout note: `plugin.yaml` intentionally lives at the repository root so
> dashboard/CLI installs using plain `ori8automations/preflight-hermes-idea-capture`
> can discover the plugin without requiring a subdirectory path.

## Layout

```
preflight-hermes-idea-capture/
├── plugin.yaml            # root plugin metadata (name, version, permissions)
├── dashboard/
│   ├── manifest.json      # dashboard manifest (tab, entry, api)
│   ├── dist/
│   │   ├── index.js       # frontend (no build step; uses the Hermes Plugin SDK)
│   │   └── style.css      # scoped styles, dark-mode first
│   └── plugin_api.py      # FastAPI router — mounted at /api/plugins/preflight-idea-capture/
└── tests/
```

## Data & storage

File-backed JSON, all under a single allow-listed data root:

```
<DATA_ROOT>/
  categories.json         # category tree + custom status/source/template definitions
  ideas/
    idea_<id>.json        # one file per idea
  attachments/            # reserved for future use
```

Default `DATA_ROOT` is `$HERMES_HOME/idea-capture` (falling back to
`~/.hermes/idea-capture`). Override it with the `PREFLIGHT_IDEA_CAPTURE_DIR`
environment variable.

Back up or migrate everything with **Manage → Data → Export JSON**, and restore
with **Import** (merge or replace).

## Safety posture

- **Constrained I/O:** the backend reads/writes **only** inside the data root.
  Every path is resolved and verified to stay within that root, and
  idea/category/status/template ids are strict slugs — so a crafted id cannot
  escape the directory via path traversal.
- **Safe import:** imported ideas are normalized and their ids validated (or
  regenerated) before writing, so an import can never write outside the root.
- **No Kanban creation:** "Draft Kanban card" only builds a draft you can copy.
  It never creates a card or dispatches anything to any external system.
- **Safe source links:** source URLs are accepted only when they are absolute
  `http://` or `https://` URLs. Unsafe schemes are blanked on create, update,
  import, export, and Kanban draft output.
- **No secrets/logs access, no external dependencies.**

## Idea record shape

```json
{
  "id": "idea_ab12cd34ef56",
  "title": "Simple AI ticket summaries for MSPs",
  "category": "product",
  "subcategory": "signals",
  "status": "parked",
  "priority": "maybe",
  "source_url": "https://…",
  "source_type": "reddit",
  "summary": "Short plain-English summary",
  "notes_markdown": "…",
  "tags": ["msp", "ai"],
  "updates": [{ "at": "2026-01-01T00:00:00Z", "by": "me", "body": "…" }],
  "created_at": "…",
  "updated_at": "…",
  "promoted_to_kanban": null
}
```

## API (mounted at `/api/plugins/preflight-idea-capture`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Data root + writability check |
| GET | `/config` | Categories, statuses, templates, priorities, source types |
| POST/PATCH/DELETE | `/categories[/{id}]` | Manage categories |
| POST/PATCH/DELETE | `/categories/{id}/subcategories[/{sid}]` | Manage subcategories |
| POST/PATCH/DELETE | `/statuses[/{id}]` | Manage custom statuses |
| POST/PATCH/DELETE | `/source-types[/{id}]` | Manage custom source types |
| POST/PATCH/DELETE | `/templates[/{id}]` | Manage item templates |
| GET | `/ideas` | List (supports `category`, `subcategory`, `status`, `source_type`, `q`, `sort`) |
| GET/POST/PATCH/DELETE | `/ideas[/{id}]` | Read/create/edit/delete ideas |
| POST | `/ideas/{id}/updates` | Append an update to the timeline |
| POST | `/ideas/{id}/promote-draft` | Generate a Kanban card draft (draft only) |
| GET | `/export` | Export the full dataset (config + all ideas) as JSON |
| POST | `/import` | Import a dataset (`mode`: `merge` or `replace`) |

### Export / import format

`GET /export` returns an object with **top-level** `config` and `ideas`:

```json
{
  "version": 1,
  "exported_at": "2026-01-01T00:00:00Z",
  "config": { "categories": [], "statuses": [], "templates": [], "source_types": [] },
  "ideas": []
}
```

`POST /import` expects those same **top-level** fields (there is no `bundle`
wrapper). `mode` is `merge` (add/overwrite by id, keep everything else) or
`replace` (wipe existing ideas first). `config` and `ideas` are both optional —
send whichever you want to import:

```json
{
  "mode": "merge",
  "config": { "categories": [], "statuses": [], "templates": [], "source_types": [] },
  "ideas": []
}
```

So an export can be re-imported as-is (the extra `version`/`exported_at` keys are
ignored). Imported config restores categories, statuses, templates, and source types.
Imported ideas are normalized, their ids are validated (or regenerated), and
unsafe source URLs are blanked, so an import can never write outside the data
root or reintroduce unsafe source links.

### Draft a Kanban card

`POST /ideas/{id}/promote-draft` builds a copy-ready card draft. The body is
optional — all three of these work:

```bash
# no body
curl -X POST .../ideas/<id>/promote-draft
# empty JSON
curl -X POST .../ideas/<id>/promote-draft -H 'Content-Type: application/json' -d '{}'
# with custom acceptance criteria
curl -X POST .../ideas/<id>/promote-draft -H 'Content-Type: application/json' \
  -d '{"acceptance_criteria": ["ships behind a flag", "has a test"]}'
```

## Tests

A self-contained smoke test exercises static checks plus the full API surface
(health, config, category/subcategory/template/idea CRUD, source-type filter,
updates, export/import round-trip, custom source-type restore into a fresh data
root, source URL scheme safety, mobile stylesheet checks, promote-draft with `{}` **and** with no body,
bad-id rejection, and path-traversal safety):

```bash
./tests/run_tests.sh
```

It runs `py_compile`, `node --check`, and a FastAPI `TestClient` suite against a
temporary `PREFLIGHT_IDEA_CAPTURE_DIR`.

The runner auto-selects a Python that has the test deps: it prefers `$PYTHON`,
then an active `$VIRTUAL_ENV`, then `/opt/hermes/.venv/bin/python`, then
`python3`. The system `python3` usually lacks `fastapi`, so if you installed
the dashboard in a venv just let it pick that up, or point at it explicitly:

```bash
PYTHON=/opt/hermes/.venv/bin/python ./tests/run_tests.sh
```

(Needs `fastapi` + `httpx` in the chosen interpreter.)

## Public release note

Preflight is an early/community Hermes Dashboard plugin. Review it in a test
Hermes environment before production use, especially if you customize the data
root or publish a modified archive. The package intentionally avoids bundled
client data, secrets, logs, and external service calls.

Built for Ori8 Automations / Hermes community use with AI-assisted drafting and
human review before publication.

## Contributing

Issues and PRs welcome. The frontend is a single dependency-free IIFE
(`dashboard/dist/index.js`) that renders with the SDK's React instance — no
build step, edit and reload. The backend is a single FastAPI router
(`dashboard/plugin_api.py`).

## License

Released under the MIT License. See [LICENSE](LICENSE).
