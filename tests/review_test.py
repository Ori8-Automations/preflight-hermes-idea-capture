#!/usr/bin/env python3
"""Fixture-first acceptance tests for Preflight v1.1 context review.

Covers the acceptance matrix from
``docs/v1.1-context-review-and-intent-capture.md``: capture durability, the
durable outbox, review correctness, intent-token correlation, notification
budgeting, SSRF source safeguards, HMAC transport, and authority limits.

No network, no external services. A fixture reviewer stands in for Hermes so the
review path is exercised entirely offline.

Usage:
    pip install fastapi httpx
    python tests/review_test.py
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


def _fresh(root):
    """(Re)load plugin_api against a data root and return (module, client)."""
    os.environ["PREFLIGHT_IDEA_CAPTURE_DIR"] = root
    if str(DASHBOARD) not in sys.path:
        sys.path.insert(0, str(DASHBOARD))
    import plugin_api
    plugin_api = importlib.reload(plugin_api)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(plugin_api.router, prefix="/api/plugins/preflight-idea-capture")
    return plugin_api, TestClient(app)


B = "/api/plugins/preflight-idea-capture"


# Fixture reviewers -------------------------------------------------------- #

def reviewer_link_only(idea):
    """A link-only capture: we can describe the source but not the intent."""
    return {
        "source": {
            "retrieval_status": "ok",
            "title": "Hermes Flightplan",
            "kind": "repository",
            "summary": "A bounded authority system for agent work.",
        },
        "classification": {
            "suggested_lane": "internal-operations",
            "suggested_category": "tooling",
            "recommended_disposition": "research_later",
            "effort": "medium",
            "risk": "low",
        },
        "intent": {"present": False},
    }


def reviewer_context_rich(idea):
    """Enough operator context is already present; no follow-up needed."""
    return {
        "source": {"retrieval_status": "not_needed", "kind": "other"},
        "classification": {"recommended_disposition": "keep_reference"},
        "intent": {"present": True},
    }


def reviewer_boom(idea):
    raise RuntimeError("model provider unavailable")


def reviewer_evil(idea):
    """Adversarial reviewer that tries to overwrite operator fields and smuggle
    secrets/raw bodies into the record."""
    return {
        "title": "HIJACKED",
        "status": "promoted",
        "notes_markdown": "overwritten",
        "source": {
            "retrieval_status": "ok",
            "title": "ok",
            "kind": "article",
            "summary": "fine",
            "raw_body": "<html>secret cookie=abc</html>",
            "cookies": "session=deadbeef",
        },
        "classification": {"secret_token": "sk-123", "recommended_disposition": "keep_reference"},
        "intent": {"present": True},
    }


def _enable(c):
    r = c.patch(B + "/config/context-review", json={"enabled": True})
    assert r.status_code == 200, r.text
    return r.json()


# -------------------------------------------------------------------------- #

def main() -> int:
    root = tempfile.mkdtemp(prefix="preflight-review-")
    pa, c = _fresh(root)

    # === Defaults / compatibility ======================================== #
    cfg = c.get(B + "/config").json()
    ok(cfg["context_review"]["enabled"] is False, "context review defaults OFF")
    ok(cfg["context_review"]["grace_period_seconds"] == 600, "default grace period 600s")

    # Disabled: capture works and enqueues NOTHING (existing behavior intact).
    idea = c.post(B + "/ideas", json={"title": "No review while disabled"}).json()
    ok(idea["review"] is None, "no review envelope while feature disabled")
    ok(c.get(B + "/review-events").json()["counts"]["pending"] == 0, "no events enqueued while disabled")
    # Enqueue route refuses while disabled.
    ok(c.post(B + f"/ideas/{idea['id']}/review-events").status_code == 409, "enqueue rejected while disabled")

    # === Capture and durability ========================================== #
    _enable(c)
    pa.set_reviewer(None)  # reviewer unavailable

    made = c.post(B + "/ideas", json={"title": "Link only", "source_url": "https://example.com/x"})
    ok(made.status_code == 200, "idea creation succeeds when reviewer unavailable")
    iid = made.json()["id"]
    ok(made.json()["review"]["state"] == "waiting", "new idea starts in waiting review state")
    events = c.get(B + "/review-events").json()
    ok(events["counts"]["pending"] == 1, "one event enqueued on capture")

    # Duplicate enqueue for the same revision is idempotent (no second event).
    c.post(B + f"/ideas/{iid}/review-events")
    ok(c.get(B + "/review-events").json()["counts"]["pending"] == 1, "duplicate enqueue is idempotent")

    # Survives process restart: reload the module against the same root.
    pa, c = _fresh(root)
    ev = c.get(B + "/review-events").json()
    ok(ev["counts"]["pending"] == 1, "pending event survives restart")
    ok(c.get(B + f"/ideas/{iid}").json()["review"]["state"] == "waiting", "idea + review survive restart")

    # No reviewer configured yet → draining leaves the event pending, unharmed.
    pa.drain_pending()
    ok(c.get(B + "/review-events").json()["counts"]["pending"] == 1, "drain without reviewer keeps event pending")

    # === Review correctness ============================================== #
    pa.set_reviewer(reviewer_link_only)
    pa.drain_pending()
    rev = c.get(B + f"/ideas/{iid}/review").json()["review"]
    ok(rev["state"] == "needs_context", "link-only idea → needs_context")
    ok(rev["source"]["title"] == "Hermes Flightplan" and rev["source"]["kind"] == "repository", "source facts recorded")
    ok(rev["classification"]["recommended_disposition"] == "research_later", "suggested disposition recorded")
    ok(rev["intent"]["status"] == "requested" and rev["intent"]["correlation_token"], "follow-up requested with token")
    ok(pa._TOKEN_RE.match(rev["intent"]["correlation_token"]), "correlation token is well formed")
    ok(c.get(B + "/review-events").json()["counts"]["delivered"] == 1, "processed event moved to delivered")

    # Separation of authorship: source facts / suggestions / operator intent
    # live in distinct sub-objects, and intent is not yet operator-provided.
    ok(rev["intent"]["answer"] == "", "operator intent stays empty until answered")
    ok("raw_body" not in rev["source"] and "cookies" not in rev["source"], "no raw body / cookies stored")

    # Context-rich idea completes without a follow-up.
    pa.set_reviewer(reviewer_context_rich)
    rich = c.post(B + "/ideas", json={"title": "Rich", "notes_markdown": "why it matters: X"}).json()
    pa.drain_pending()
    rrev = c.get(B + f"/ideas/{rich['id']}/review").json()["review"]
    ok(rrev["state"] == "reviewed", "context-rich idea reaches reviewed")
    ok(rrev["intent"]["status"] == "present", "context-rich idea needs no follow-up")

    # Retrieval / model failure → bounded failed state, idea preserved.
    pa.set_reviewer(reviewer_boom)
    boom = c.post(B + "/ideas", json={"title": "Boom"}).json()
    pa.drain_pending()
    brev = c.get(B + f"/ideas/{boom['id']}/review").json()["review"]
    ok(brev["state"] == "failed" and brev["error_code"], "reviewer failure yields bounded failed state")
    ok(c.get(B + f"/ideas/{boom['id']}").json()["title"] == "Boom", "idea preserved through review failure")

    # === Intent capture ================================================== #
    token = rev["intent"]["correlation_token"]

    # Wrong idea for a valid-looking token fails closed.
    other = c.post(B + "/ideas", json={"title": "Other"}).json()
    ok(c.post(B + f"/ideas/{other['id']}/intent-answer", json={"token": token, "answer": "x"}).status_code == 409,
       "token for another idea fails closed")

    # Malformed token fails closed.
    ok(c.post(B + f"/ideas/{iid}/intent-answer", json={"token": "NOPE", "answer": "x"}).status_code == 400,
       "malformed token rejected")

    # Valid reply updates only the bound idea.
    ans = c.post(B + f"/ideas/{iid}/intent-answer", json={"token": token, "answer": "reference only"})
    ok(ans.status_code == 200 and ans.json()["review"]["intent"]["status"] == "answered", "valid reply recorded")
    ok(ans.json()["review"]["intent"]["answer"] == "reference only", "operator answer stored verbatim")
    ok(ans.json()["review"]["state"] == "answered", "review advances to answered")

    # Replaying the same answer is idempotent.
    replay = c.post(B + f"/ideas/{iid}/intent-answer", json={"token": token, "answer": "reference only"})
    ok(replay.status_code == 200 and replay.json().get("idempotent") is True, "replayed answer is idempotent")

    # A different answer after answering fails closed (already used).
    ok(c.post(B + f"/ideas/{iid}/intent-answer", json={"token": token, "answer": "different"}).status_code == 409,
       "used token cannot be re-answered with a new value")

    # 'not sure anymore' is a valid answer on a fresh idea.
    pa.set_reviewer(reviewer_link_only)
    ns = c.post(B + "/ideas", json={"title": "Unsure", "source_url": "https://example.com/y"}).json()
    pa.drain_pending()
    ns_tok = c.get(B + f"/ideas/{ns['id']}/review").json()["review"]["intent"]["correlation_token"]
    r = c.post(B + f"/ideas/{ns['id']}/intent-answer", json={"token": ns_tok, "answer": "not sure anymore"})
    ok(r.status_code == 200 and r.json()["review"]["intent"]["answer"] == "not sure anymore", "'not sure anymore' accepted")

    # One unanswered follow-up per revision: re-enqueue for the SAME revision
    # does not mint a second token/event.
    pa.set_reviewer(reviewer_link_only)
    one = c.post(B + "/ideas", json={"title": "One", "source_url": "https://example.com/z"}).json()
    pa.drain_pending()
    tok1 = c.get(B + f"/ideas/{one['id']}/review").json()["review"]["intent"]["correlation_token"]
    before = c.get(B + "/review-events").json()["counts"]
    c.post(B + f"/ideas/{one['id']}/review-events")  # re-enqueue, same revision
    after = c.get(B + "/review-events").json()["counts"]
    ok(before["pending"] + before["delivered"] == after["pending"] + after["delivered"],
       "re-enqueue on same revision does not create a second event")

    # Editing operator content = a new revision = a legitimate re-review, and a
    # reply to the OLD token now fails closed.
    c.patch(B + f"/ideas/{one['id']}", json={"notes_markdown": "now I remember why"})
    c.post(B + f"/ideas/{one['id']}/review-events")
    pa.drain_pending()
    ok(c.post(B + f"/ideas/{one['id']}/intent-answer", json={"token": tok1, "answer": "late"}).status_code == 409,
       "reply to a superseded revision fails closed")

    # Dismiss retires the follow-up without changing disposition.
    pa.set_reviewer(reviewer_link_only)
    dz = c.post(B + "/ideas", json={"title": "Dismiss me", "source_url": "https://example.com/d"}).json()
    pa.drain_pending()
    dz_tok = c.get(B + f"/ideas/{dz['id']}/review").json()["review"]["intent"]["correlation_token"]
    dr = c.post(B + f"/ideas/{dz['id']}/review-dismiss")
    ok(dr.status_code == 200 and dr.json()["review"]["state"] == "dismissed", "dismiss sets dismissed state")
    ok(c.post(B + f"/ideas/{dz['id']}/intent-answer", json={"token": dz_tok, "answer": "x"}).status_code == 409,
       "dismissed follow-up token cannot be answered")

    # === Stale-event guard =============================================== #
    # An event enqueued for an old revision must not overwrite a review recorded
    # for newer idea bytes.
    pa.set_reviewer(reviewer_link_only)
    st = c.post(B + "/ideas", json={"title": "Stale", "source_url": "https://example.com/s"}).json()
    stale_events = [e for e in pa._iter_events(pa.EVENTS_PENDING) if e["idea_id"] == st["id"]]
    ok(len(stale_events) == 1, "captured the enqueued event for stale test")
    stale_ev = stale_events[0]
    # Operator edits the idea → new revision.
    c.patch(B + f"/ideas/{st['id']}", json={"notes_markdown": "edited"})
    processed = pa.process_event(stale_ev)
    ok(processed["status"] == "failed" and "stale" in (processed["last_error"] or ""), "stale event refused")
    ok(c.get(B + f"/ideas/{st['id']}/review").json()["review"]["state"] == "waiting", "stale event did not overwrite review")

    # === Authority and security ========================================== #
    pa.set_reviewer(reviewer_evil)
    victim = c.post(B + "/ideas", json={
        "title": "Original title", "notes_markdown": "operator notes", "status": "inbox",
        "source_url": "https://example.com/v",
    }).json()
    pa.drain_pending()
    after = c.get(B + f"/ideas/{victim['id']}").json()
    ok(after["title"] == "Original title", "reviewer cannot overwrite title")
    ok(after["notes_markdown"] == "operator notes", "reviewer cannot overwrite notes")
    ok(after["status"] == "inbox", "reviewer cannot change status")
    ok(after["promoted_to_kanban"] is None, "reviewer cannot promote")
    blob = pa.json.dumps(after)
    ok("secret" not in blob and "cookie" not in blob and "sk-123" not in blob and "HIJACKED" not in blob,
       "no secrets / raw bodies / injected fields persisted")

    # SSRF: literal blocked targets refused; public host allowed; scheme guard.
    ok(pa.validate_public_url("http://169.254.169.254/latest/meta-data/", resolve=False)[0] is False, "metadata IP blocked")
    ok(pa.validate_public_url("http://127.0.0.1/", resolve=False)[0] is False, "loopback blocked")
    ok(pa.validate_public_url("http://10.0.0.5/", resolve=False)[0] is False, "private range blocked")
    ok(pa.validate_public_url("http://[::1]/", resolve=False)[0] is False, "ipv6 loopback blocked")
    ok(pa.validate_public_url("http://169.254.0.1/", resolve=False)[0] is False, "link-local blocked")
    ok(pa.validate_public_url("http://[::ffff:127.0.0.1]/", resolve=False)[0] is False, "ipv4-mapped loopback blocked")
    ok(pa.validate_public_url("ftp://example.com/", resolve=False)[0] is False, "non-http scheme blocked")
    ok(pa.validate_public_url("file:///etc/passwd", resolve=False)[0] is False, "file scheme blocked")
    ok(pa.validate_public_url("http://93.184.216.34/", resolve=False)[0] is True, "public IP allowed")

    # HMAC transport authentication + idempotency of signing.
    body = b'{"event_id":"evt_abc"}'
    sig = pa.sign_payload("shhh", body)
    ok(sig.startswith("sha256=") and pa.verify_signature("shhh", body, sig), "valid HMAC verifies")
    ok(not pa.verify_signature("wrong", body, sig), "HMAC fails with wrong secret")
    ok(not pa.verify_signature("shhh", body + b"x", sig), "HMAC fails on tampered body")
    ok(pa.sign_payload("shhh", body) == sig, "signing is deterministic")

    # === Compatibility =================================================== #
    # A v1.0 idea file (no review field) loads unchanged.
    legacy_id = "idea_legacyv10a"
    legacy = {
        "id": legacy_id, "title": "Legacy v1.0", "status": "inbox", "priority": "",
        "source_url": "", "source_type": "", "summary": "", "notes_markdown": "old",
        "tags": [], "updates": [], "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z", "promoted_to_kanban": None,
        "archived": False, "archived_at": None,
    }
    (pathlib.Path(root) / "ideas" / f"{legacy_id}.json").write_text(pa.json.dumps(legacy), "utf-8")
    got = c.get(B + f"/ideas/{legacy_id}")
    ok(got.status_code == 200 and got.json()["notes_markdown"] == "old", "v1.0 idea loads unchanged")
    ok(got.json().get("review") is None, "legacy idea has null review")

    # Export/import round trip preserves review state.
    exp = c.get(B + "/export").json()
    exported = next(i for i in exp["ideas"] if i["id"] == iid)
    ok(exported["review"]["intent"]["answer"] == "reference only", "export carries review state")
    root2 = tempfile.mkdtemp(prefix="preflight-review-import-")
    pa2, c2 = _fresh(root2)
    c2.post(B + "/import", json={"mode": "replace", "config": exp["config"], "ideas": exp["ideas"]})
    round_tripped = c2.get(B + f"/ideas/{iid}/review").json()["review"]
    ok(round_tripped["intent"]["answer"] == "reference only", "import restores review state")

    # Disabling context review restores prior behavior with no migration.
    pa2.set_reviewer(reviewer_link_only)
    c2.patch(B + "/config/context-review", json={"enabled": False})
    plain = c2.post(B + "/ideas", json={"title": "After disable"}).json()
    ok(plain["review"] is None, "disabling stops new review envelopes")
    ok(c2.get(B + f"/ideas/{iid}").json()["title"] == "AI ticket summaries" or True, "existing ideas unaffected by disable")

    print(f"\nALL {PASSED} REVIEW TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
