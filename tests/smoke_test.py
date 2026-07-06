#!/usr/bin/env python3
"""Self-contained smoke test for the Preflight Idea Capture backend.

Runs the FastAPI router against a throwaway data root and asserts the full API
surface behaves. No network, no external services — just fastapi + httpx.

Usage:
    pip install fastapi httpx
    python tests/smoke_test.py
"""

import importlib
import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
DASHBOARD = HERE.parent / "dashboard"

PASSED = 0


def ok(cond, msg):
    global PASSED
    print(("PASS" if cond else "FAIL"), msg)
    if not cond:
        raise AssertionError(msg)
    PASSED += 1


def main() -> int:
    root = tempfile.mkdtemp(prefix="preflight-smoke-")
    os.environ["PREFLIGHT_IDEA_CAPTURE_DIR"] = root
    sys.path.insert(0, str(DASHBOARD))

    import plugin_api  # noqa: E402  (import after sys.path/env setup)
    importlib.reload(plugin_api)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(plugin_api.router, prefix="/api/plugins/preflight-idea-capture")
    c = TestClient(app)
    B = "/api/plugins/preflight-idea-capture"

    # --- health & config ---
    ok(c.get(B + "/health").json()["ok"], "health")
    cfg = c.get(B + "/config").json()
    ok(cfg["categories"] == [], "config: no seeded categories")
    ok(len(cfg["statuses"]) >= 1, "config: statuses present")
    ok(len(cfg["templates"]) >= 1, "config: templates present")
    ok(any(s["id"] == "reddit" for s in cfg["source_types"]), "config: source types present")

    # --- taxonomy CRUD ---
    cat = c.post(B + "/categories", json={"name": "Product"}).json()
    ok(cat["id"] == "product", "create category")
    sub = c.post(B + f"/categories/{cat['id']}/subcategories", json={"name": "Signals"}).json()
    ok(sub["id"] == "signals", "create subcategory")
    tpl = c.post(B + "/templates", json={"name": "Bug", "status": "inbox", "tags": ["bug"]}).json()
    ok(tpl["id"] == "bug", "create template")

    # source types: config-backed CRUD
    ok(c.get(B + "/config").json()["source_types"][0]["id"] == "", "source types lead with unset option")
    src = c.post(B + "/source-types", json={"label": "Discord", "emoji": "🎮"}).json()
    ok(src["id"] == "discord", "create source type")
    ok(c.patch(B + "/source-types/" + src["id"], json={"emoji": "👾"}).json()["emoji"] == "👾", "edit source type")
    tmp_src = c.post(B + "/source-types", json={"label": "Temporary", "emoji": "🧪"}).json()
    ok(c.delete(B + "/source-types/" + tmp_src["id"]).status_code == 200, "delete source type")

    # --- ideas ---
    idea = c.post(B + "/ideas", json={
        "title": "AI ticket summaries",
        "category": cat["id"], "subcategory": sub["id"],
        "source_type": src["id"], "source_url": "https://example.com/signal", "tags": ["msp"],
    }).json()
    iid = idea["id"]
    ok(iid.startswith("idea_") and idea["source_type"] == src["id"], "create idea")
    ok(idea["source_url"] == "https://example.com/signal", "https source_url preserved on create")
    ok(c.get(B + "/ideas?source_type=" + src["id"]).json()["count"] == 1, "filter by source_type")

    unsafe = c.post(B + "/ideas", json={"title": "Unsafe URL", "source_url": "javascript:alert(1)"}).json()
    ok(unsafe["source_url"] == "", "unsafe source_url blanked on create")
    safe_update = c.patch(B + f"/ideas/{unsafe['id']}", json={"source_url": "https://example.com/safe"}).json()
    ok(safe_update["source_url"] == "https://example.com/safe", "https source_url preserved on update")
    unsafe_update = c.patch(B + f"/ideas/{unsafe['id']}", json={"source_url": "data:text/html,<svg>"}).json()
    ok(unsafe_update["source_url"] == "", "unsafe source_url blanked on update")
    c.delete(B + f"/ideas/{unsafe['id']}")

    upd = c.post(B + f"/ideas/{iid}/updates", json={"body": "looked into it"}).json()
    ok(upd["updates"][-1]["by"] == "me", "append update (default author 'me')")

    # --- export / import round-trip ---
    exp = c.get(B + "/export").json()
    ok("config" in exp and "ideas" in exp and len(exp["ideas"]) == 1, "export bundle (top-level config/ideas)")
    ok(any(s["id"] == src["id"] for s in exp["config"].get("source_types", [])), "export includes custom source types")
    ok(c.post(B + "/import", json={"mode": "replace", "ideas": []}).status_code == 200, "import replace clears")
    ok(c.get(B + "/ideas").json()["count"] == 0, "ideas cleared")
    imp = c.post(B + "/import", json={"mode": "merge", "config": exp["config"], "ideas": exp["ideas"]}).json()
    ok(imp["ideas_written"] == 1, "import merge writes ideas")
    ok(c.get(B + f"/ideas?q=summaries").json()["count"] == 1, "imported idea is searchable")
    ok(c.get(B + "/ideas?source_type=" + src["id"]).json()["count"] == 1, "imported idea keeps custom source type")

    # --- promote-draft: empty JSON body AND omitted body ---
    d1 = c.post(B + f"/ideas/{iid}/promote-draft", json={})
    ok(d1.status_code == 200 and "# AI ticket summaries" in d1.json()["draft"]["markdown"], "promote-draft with {} body")
    d2 = c.post(B + f"/ideas/{iid}/promote-draft")  # no body at all
    ok(d2.status_code == 200 and d2.json()["draft"]["idea_ref"] == iid, "promote-draft with omitted body")
    d3 = c.post(B + f"/ideas/{iid}/promote-draft", json={"acceptance_criteria": ["ships behind a flag"]})
    ok("- [ ] ships behind a flag" in d3.json()["draft"]["markdown"], "promote-draft with custom criteria")

    # --- static UX guardrails ---
    css = (DASHBOARD / "dist" / "style.css").read_text(encoding="utf-8")
    ok("@media (max-width: 720px)" in css, "mobile breakpoint present")
    ok("flex-direction: column" in css and "overflow: visible" in css, "mobile workspace stacks vertically")
    ok("overflow-x: auto" in css and "ic-sidebar" in css, "mobile category chips can scroll horizontally")
    ok("width: 100vw" in css and "height: 100dvh" in css, "mobile detail pane is full-screen")
    ok("-webkit-line-clamp: 4" in css, "mobile cards clamp long summaries")
    ok("max-height: min(52vh, 560px)" in css and "overscroll-behavior: contain" in css, "long markdown preview scrolls internally")
    ok("grid-template-columns: minmax(0, 1fr) auto" in css and "text-overflow: ellipsis" in css, "mobile detail title does not overlap actions")

    # --- safety: bad id + path traversal ---
    ok(c.get(B + "/ideas/idea_BAD").status_code == 400, "bad idea id rejected")
    ok(c.get(B + "/ideas/..%2f..%2fetc%2fpasswd").status_code in (400, 404), "encoded traversal rejected")
    bad = c.post(B + "/import", json={"mode": "merge", "ideas": [{"id": "../../evil", "title": "pwn"}]}).json()
    ok(bad["ideas_written"] == 1, "malformed-id idea imported safely (regenerated)")
    files = [str(p) for p in pathlib.Path(root).rglob("*") if p.is_file()]
    ok(all(f.startswith(root) for f in files), "no escaped files")
    ok(not any("evil" in f for f in files), "no file named from traversal id")

    imported_bad_url = c.post(B + "/import", json={
        "mode": "merge",
        "ideas": [{"id": "idea_badurl1", "title": "Unsafe import URL", "source_url": "javascript:alert(1)"}],
    }).json()
    ok(imported_bad_url["ideas_written"] == 1, "unsafe-url idea imported")
    clean_import = c.get(B + "/ideas/idea_badurl1").json()
    ok(clean_import["source_url"] == "", "unsafe source_url blanked on import")
    draft_bad_url = c.post(B + "/ideas/idea_badurl1/promote-draft", json={}).json()["draft"]
    ok(draft_bad_url["source_url"] == "" and "javascript:" not in draft_bad_url["markdown"], "promote-draft excludes unsafe source_url")

    # Import into a fresh data root: custom source types must be restored before
    # ideas are normalized, otherwise ideas keep only raw ids without labels.
    root2 = tempfile.mkdtemp(prefix="preflight-smoke-restore-")
    os.environ["PREFLIGHT_IDEA_CAPTURE_DIR"] = root2
    plugin_api = importlib.reload(plugin_api)
    app2 = FastAPI()
    app2.include_router(plugin_api.router, prefix="/api/plugins/preflight-idea-capture")
    c2 = TestClient(app2)
    imp2 = c2.post(B + "/import", json={"mode": "replace", "config": exp["config"], "ideas": exp["ideas"]}).json()
    ok(imp2["ideas_written"] == 1 and imp2["config_updated"], "fresh replace import writes config and ideas")
    cfg2 = c2.get(B + "/config").json()
    ok(any(s["id"] == src["id"] for s in cfg2["source_types"]), "fresh replace import restores custom source types")
    ok(c2.get(B + "/ideas?source_type=" + src["id"]).json()["count"] == 1, "fresh replace import preserves custom idea source_type")

    print(f"\nALL {PASSED} SMOKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
