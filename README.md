# Preflight — Hermes Idea Capture

Hermes Dashboard plugin for lightweight idea capture — categories, subcategories, notes, statuses, sources, and updates before work becomes Kanban.

> **Early / community package.** Preflight is functional and in active use, but the Hermes dashboard plugin API surface may change before a 1.0 release. Pin your version if you need stability.

## Why this exists

Hermes already has Kanban for active agent execution. That is exactly the problem: not every thought deserves to become executable work the moment it appears.

**Ideas need a staging area.** Random product ideas, market signals, research links, and “maybe later” work should be easy to capture without cluttering an execution board. Preflight gives those rough signals a structured shelf:

```text
Category → Subcategory → Idea → notes · status · source · updates
```

**Agent workflows need a pre-execution layer.** Most project tools push you from idea straight to task. Preflight keeps the front of the pipeline intentionally quieter:

```text
signal / thought / maybe
  → structured idea
  → candidate work
  → human-approved Kanban action
  → agent execution
```

Preflight owns the first two steps and stops there. “Draft Kanban card” creates copy-ready text only — it does not dispatch an agent, create a card, or call an external service.

**Full project management is out of scope.** No sprints, no assignments, no workload reports, no external trackers. This is a local idea shelf for operators who want Mission Control without buying a second cockpit.

## Install

Copy the plugin directory into your Hermes plugins path, enable it, and restart the dashboard:

```bash
cp -r preflight-idea-capture ~/.hermes/plugins/
hermes plugins enable preflight-idea-capture
hermes dashboard --host 127.0.0.1 --port 9119 --no-open
```

Manual config fallback:

```yaml
plugins:
  enabled:
    - preflight-idea-capture
```

Then restart the dashboard, or rescan if it is already running:

```bash
curl http://127.0.0.1:9119/api/dashboard/plugins/rescan
```

The **Preflight** tab appears in the dashboard navigation.

## Data setup

Preflight stores JSON files under one data root:

```text
<DATA_ROOT>/
  categories.json
  ideas/
    idea_<id>.json
  attachments/
```

Default root:

```text
$HERMES_HOME/idea-capture
```

Override it when starting the dashboard:

```bash
PREFLIGHT_IDEA_CAPTURE_DIR=/path/to/idea-capture hermes dashboard --host 127.0.0.1 --port 9119 --no-open
```

Back up or migrate data with **Manage → Data → Export JSON**, then restore with **Import**.

## Features

| Area | What it does |
|---|---|
| Quick capture | Single-line capture bar for dumping ideas quickly |
| Categories | Organize ideas by category and subcategory |
| Statuses | Inbox, Parked, Researching, Candidate, Promoted, Discarded; editable |
| Source tracking | Source URL and source type such as Reddit, web, internal, client, email |
| Templates | Prefill status, priority, tags, source type, and note scaffolds |
| Notes | Markdown notes with live preview |
| Updates | Append-only update timeline; status changes are auto-logged |
| Export/import | Move the whole dataset as JSON |
| Kanban draft | Generate a copy-ready card draft, without creating anything automatically |

## API

Mounted by Hermes at:

```text
/api/plugins/preflight-idea-capture
```

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Check data root and writability |
| GET | `/config` | Categories, statuses, templates, priorities, source types |
| POST/PATCH/DELETE | `/categories[/{id}]` | Manage categories |
| POST/PATCH/DELETE | `/categories/{id}/subcategories[/{sid}]` | Manage subcategories |
| POST/PATCH/DELETE | `/statuses[/{id}]` | Manage custom statuses |
| POST/PATCH/DELETE | `/templates[/{id}]` | Manage item templates |
| GET | `/ideas` | List ideas; supports `category`, `subcategory`, `status`, `source_type`, `q`, `sort` |
| GET/POST/PATCH/DELETE | `/ideas[/{id}]` | Read, create, edit, delete ideas |
| POST | `/ideas/{id}/updates` | Append an update |
| POST | `/ideas/{id}/promote-draft` | Generate a Kanban-card draft only |
| GET | `/export` | Export config and ideas as JSON |
| POST | `/import` | Import config and/or ideas with `merge` or `replace` mode |

## Export / import format

`GET /export` returns top-level `config` and `ideas`:

```json
{
  "version": 1,
  "exported_at": "2026-01-01T00:00:00Z",
  "config": { "categories": [], "statuses": [], "templates": [] },
  "ideas": []
}
```

`POST /import` expects the same top-level fields. There is no `bundle` wrapper:

```json
{
  "mode": "merge",
  "config": { "categories": [], "statuses": [], "templates": [] },
  "ideas": []
}
```

`mode` is either `merge` or `replace`. Extra export metadata such as `version` and `exported_at` is ignored on import.

## Draft Kanban card

`POST /ideas/{id}/promote-draft` builds copy-ready text only. The request body is optional:

```bash
# no body
curl -X POST .../ideas/<id>/promote-draft

# empty JSON
curl -X POST .../ideas/<id>/promote-draft -H 'Content-Type: application/json' -d '{}'

# with custom acceptance criteria
curl -X POST .../ideas/<id>/promote-draft -H 'Content-Type: application/json' \
  -d '{"acceptance_criteria": ["ships behind a flag", "has a test"]}'
```

## ⚠️ Write operations warning

This plugin includes **local write operations** inside its configured idea-capture data root.

- Do not point `PREFLIGHT_IDEA_CAPTURE_DIR` at a directory containing unrelated data.
- Do not expose the plugin API directly to untrusted users or unaudited agent tools.
- Import can overwrite local idea data when used in `replace` mode.
- “Draft Kanban card” does **not** create a Kanban card, dispatch an agent, or call any external service.

## Known limitations

- File-backed JSON is intentionally simple; it is not a multi-user database.
- Attachments are reserved in the data layout but not implemented yet.
- Markdown preview is lightweight, not a full GitHub-flavored Markdown renderer.
- There is no built-in authentication layer beyond your Hermes dashboard deployment.

## Tests

```bash
./tests/run_tests.sh
```

The runner auto-selects a Python interpreter that has the dashboard test dependencies. It checks `$PYTHON`, an active `$VIRTUAL_ENV`, `/opt/hermes/.venv/bin/python`, then `python3` / `python`.

To force the Hermes venv:

```bash
PYTHON=/opt/hermes/.venv/bin/python ./tests/run_tests.sh
```

The suite runs `py_compile`, `node --check` if available, and a FastAPI `TestClient` smoke test covering config, category/subcategory/template/idea CRUD, source-type filtering, updates, export/import, draft promotion, and path-traversal safety.

## Credits

Built by [Claude](https://claude.ai) (Anthropic) under the direction of **Ori8**, the Hermes-based AI agent at the core of [Ori8 Automations](https://github.com/ori8automations). A human provided requirements, review, and final approval.

## License

Released under the MIT License. See [LICENSE](LICENSE).
