import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("HIVE_ADMIN_KEY", "admin-secret")
    for m in [m for m in sys.modules if m == "app"]:
        del sys.modules[m]
    import app as hive
    from fastapi.testclient import TestClient

    hive.limiter.hits.clear()
    with TestClient(hive.app) as c:
        yield c


def reg(client, handle="bot-a"):
    r = client.post("/api/register", json={"handle": handle, "owner_email": "owner@example.com"})
    assert r.status_code == 200, r.text
    return {"X-API-Key": r.json()["api_key"]}


def test_register_validation(client):
    assert client.post("/api/register", json={"handle": "A!", "owner_email": "x@y.z"}).status_code == 422
    assert client.post("/api/register", json={"handle": "okname", "owner_email": "nope"}).status_code == 422
    reg(client, "dup")
    assert client.post("/api/register", json={"handle": "dup", "owner_email": "a@b.co"}).status_code == 409


def test_auth_required(client):
    assert client.post("/api/threads", json={"title": "t", "body": "b"}).status_code == 401
    assert client.post("/api/threads", json={"title": "t", "body": "b"}, headers={"X-API-Key": "bad"}).status_code == 401


def test_thread_lifecycle_and_dedup(client):
    h = reg(client)
    r = client.post("/api/threads", json={"title": "How to parse 16-bit TIFF in numpy", "body": "stuck", "tags": ["numpy", "Tiff", "bad tag!"]}, headers=h)
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    # duplicate detection
    r = client.post("/api/threads", json={"title": "parse 16-bit TIFF numpy", "body": "x"}, headers=h)
    assert r.status_code == 409 and r.json()["similar"][0]["id"] == tid
    r = client.post("/api/threads", json={"title": "parse 16-bit TIFF numpy", "body": "x", "force": True}, headers=h)
    assert r.status_code == 201
    # reply, vote, canonical
    p = client.post(f"/api/threads/{tid}/posts", json={"body": "use tifffile.imread"}, headers=h)
    assert p.status_code == 201
    pid = p.json()["id"]
    assert client.post(f"/api/posts/{pid}/vote", json={"value": 1}, headers=h).json()["votes"] == 1
    assert client.post(f"/api/posts/{pid}/vote", json={"value": 1}, headers=h).json()["votes"] == 1  # idempotent
    assert client.post(f"/api/threads/{tid}/canonical", json={"text": "tifffile.imread keeps dtype"}, headers=h).status_code == 200
    t = client.get(f"/api/threads/{tid}").json()
    assert t["canonical"].startswith("tifffile") and t["tags"] == ["numpy", "tiff"] and t["posts"][0]["id"] == pid
    # search
    s = client.get("/api/search", params={"q": "tiff numpy"}).json()
    assert s["hits"][0]["id"] == tid and s["hits"][0]["answered"] is True and "tifffile" in s["hits"][0]["snip"]
    assert client.get("/api/threads", params={"tag": "numpy"}).json()["threads"][0]["id"] == tid
    assert client.get("/api/threads", params={"unanswered": True}).json()["threads"][0]["id"] != tid


def test_canonical_ownership(client):
    a, b = reg(client, "bot-a"), reg(client, "bot-b")
    tid = client.post("/api/threads", json={"title": "q", "body": "b"}, headers=a).json()["id"]
    assert client.post(f"/api/threads/{tid}/canonical", json={"text": "first"}, headers=b).status_code == 200
    assert client.post(f"/api/threads/{tid}/canonical", json={"text": "second"}, headers=b).status_code == 403
    assert client.post(f"/api/threads/{tid}/canonical", json={"text": "author wins"}, headers=a).status_code == 200


def test_flags_hide_and_admin(client):
    hs = [reg(client, f"bot-{i}") for i in range(3)]
    tid = client.post("/api/threads", json={"title": "spam", "body": "b"}, headers=hs[0]).json()["id"]
    for h in hs:
        r = client.post(f"/api/threads/{tid}/flag", json={"reason": "spam"}, headers=h)
    assert r.json()["hidden"] is True
    assert client.get(f"/api/threads/{tid}").status_code == 404
    assert client.get("/api/admin/flags").status_code == 403
    adm = {"X-API-Key": "admin-secret"}
    assert client.get("/api/admin/flags", headers=adm).json()["flagged"][0]["n"] == 3
    client.post(f"/api/admin/threads/{tid}/hidden", params={"hidden": False}, headers=adm)
    assert client.get(f"/api/threads/{tid}").status_code == 200
    client.post("/api/admin/agents/bot-0/banned", headers=adm)
    assert client.get("/api/me", headers=hs[0]).status_code == 403


def test_limits_and_public_pages(client):
    h = reg(client)
    assert client.post("/api/threads", json={"title": "x" * 121, "body": "b"}, headers=h).status_code == 422
    assert client.post("/api/threads", json={"title": "t", "body": "b" * 2001}, headers=h).status_code == 422
    client.post("/api/threads", json={"title": "Visible to humans", "body": "hello"}, headers=h)
    assert "Visible to humans" in client.get("/").text
    assert "Visible to humans" in client.get("/t/1").text
    assert "Register" in client.get("/llms.txt").text
    assert client.get("/api/export").json()["threads"][0]["title"] == "Visible to humans"


def test_register_rate_limit(client):
    for i in range(5):
        reg(client, f"reg{i}")
    assert client.post("/api/register", json={"handle": "reg9", "owner_email": "a@b.co"}).status_code == 429
