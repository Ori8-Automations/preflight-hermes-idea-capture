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
DASHBOARD = HERE.parent / "preflight-idea-capture" / "dashboard"

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

    # --- ideas ---
    idea = c.post(B + "/ideas", json={
        "title": "AI ticket summaries",
        "category": cat["id"], "subcategory": sub["id"],
        "source_type": "reddit", "tags": ["msp"],
    }).json()
    iid = idea["id"]
    ok(iid.startswith("idea_") and idea["source_type"] == "reddit", "create idea")
    ok(c.get(B + "/ideas?source_type=reddit").json()["count"] == 1, "filter by source_type")

    upd = c.post(B + f"/ideas/{iid}/updates", json={"body": "looked into it"}).json()
    ok(upd["updates"][-1]["by"] == "me", "append update (default author 'me')")

    # --- export / import round-trip ---
    exp = c.get(B + "/export").json()
    ok("config" in exp and "ideas" in exp and len(exp["ideas"]) == 1, "export bundle (top-level config/ideas)")
    ok(c.post(B + "/import", json={"mode": "replace", "ideas": []}).status_code == 200, "import replace clears")
    ok(c.get(B + "/ideas").json()["count"] == 0, "ideas cleared")
    imp = c.post(B + "/import", json={"mode": "merge", "config": exp["config"], "ideas": exp["ideas"]}).json()
    ok(imp["ideas_written"] == 1, "import merge writes ideas")
    ok(c.get(B + f"/ideas?q=summaries").json()["count"] == 1, "imported idea is searchable")

    # --- promote-draft: empty JSON body AND omitted body ---
    d1 = c.post(B + f"/ideas/{iid}/promote-draft", json={})
    ok(d1.status_code == 200 and "# AI ticket summaries" in d1.json()["draft"]["markdown"], "promote-draft with {} body")
    d2 = c.post(B + f"/ideas/{iid}/promote-draft")  # no body at all
    ok(d2.status_code == 200 and d2.json()["draft"]["idea_ref"] == iid, "promote-draft with omitted body")
    d3 = c.post(B + f"/ideas/{iid}/promote-draft", json={"acceptance_criteria": ["ships behind a flag"]})
    ok("- [ ] ships behind a flag" in d3.json()["draft"]["markdown"], "promote-draft with custom criteria")

    # --- safety: bad id + path traversal ---
    ok(c.get(B + "/ideas/idea_BAD").status_code == 400, "bad idea id rejected")
    ok(c.get(B + "/ideas/..%2f..%2fetc%2fpasswd").status_code in (400, 404), "encoded traversal rejected")
    bad = c.post(B + "/import", json={"mode": "merge", "ideas": [{"id": "../../evil", "title": "pwn"}]}).json()
    ok(bad["ideas_written"] == 1, "malformed-id idea imported safely (regenerated)")
    files = [str(p) for p in pathlib.Path(root).rglob("*") if p.is_file()]
    ok(all(f.startswith(root) for f in files), "no escaped files")
    ok(not any("evil" in f for f in files), "no file named from traversal id")

    print(f"\nALL {PASSED} SMOKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
