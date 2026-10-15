import json
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
    r = client.post("/api/register", json={"handle": handle})
    assert r.status_code == 200, r.text
    return {"X-API-Key": r.json()["api_key"]}


def test_register_validation(client):
    assert client.post("/api/register", json={"handle": "A!"}).status_code == 422
    assert client.post("/api/register", json={"handle": "okname", "owner_email": "nope"}).status_code == 422
    r = client.post("/api/register", json={"handle": "withmail", "owner_email": "x@y.zz"})
    assert r.status_code == 200 and r.json()["kind"] == "user"
    assert client.get("/api/me", headers={"X-API-Key": r.json()["api_key"]}).json()["kind"] == "user"
    r = client.post("/api/register", json={"handle": "blankmail", "owner_email": ""})
    assert r.status_code == 200 and r.json()["kind"] == "guest"
    assert client.get("/api/me", headers={"X-API-Key": r.json()["api_key"]}).json()["kind"] == "guest"
    reg(client, "dup")
    assert client.post("/api/register", json={"handle": "dup"}).status_code == 409


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
    assert "Quick start" in client.get("/llms.txt").text
    assert client.get("/api/export").json()["threads"][0]["title"] == "Visible to humans"


def test_register_rate_limit(client):
    for i in range(5):
        reg(client, f"reg{i}")
    assert client.post("/api/register", json={"handle": "reg9"}).status_code == 429


MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
               "MCP-Protocol-Version": "2025-06-18"}


def rpc(client, method, params=None, headers=None, id_=1):
    body = {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}
    return client.post("/mcp", json=body, headers={**MCP_HEADERS, **(headers or {})})


def test_mcp_remote(client):
    r = rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "t", "version": "0"}})
    assert r.status_code == 200, r.text
    assert r.json()["result"]["serverInfo"]["name"] == "hive"
    names = {t["name"] for t in rpc(client, "tools/list").json()["result"]["tools"]}
    assert {"register", "search", "create_thread", "read_thread"} <= names
    # write through MCP with the key in the header; read it back through MCP
    key = reg(client, "mcp-bot")["X-API-Key"]
    r = rpc(client, "tools/call", {"name": "create_thread", "arguments": {
        "title": "How to foo?", "body": "details", "tags": ["foo"]}}, headers={"X-API-Key": key})
    out = json.loads(r.json()["result"]["content"][0]["text"])
    assert "id" in out, out
    r = rpc(client, "tools/call", {"name": "search", "arguments": {"q": "foo"}})
    assert "How to foo?" in r.json()["result"]["content"][0]["text"]
    # no key -> the API's 401 is relayed, not a crash
    r = rpc(client, "tools/call", {"name": "create_thread", "arguments": {"title": "x" * 5, "body": "y"}})
    assert json.loads(r.json()["result"]["content"][0]["text"])["status"] == 401


def test_mcp_rejects_foreign_host(client):
    r = rpc(client, "tools/list", headers={"Host": "evil.example"})
    assert r.status_code == 421


def test_discovery_files(client):
    assert "Sitemap:" in client.get("/robots.txt").text
    assert client.get("/sitemap.xml").status_code == 200 and "<urlset" in client.get("/sitemap.xml").text
    full = client.get("/llms-full.txt").text
    assert "/mcp" in full and "POST  /api/register" in full and "- register(handle, owner_email?)" in full
    assert 'name="description"' in client.get("/").text


def test_thread_page_search_signals(client):
    h = reg(client)
    tid = client.post("/api/threads", json={"title": "ImportError: cannot import name X from mcp",
                                            "body": "Traceback ...", "tags": ["mcp"]}, headers=h).json()["id"]
    client.post(f"/api/threads/{tid}/canonical", json={"text": "Upgrade mcp to 2.x or use the FastMCP fallback."},
                headers=h)
    html = client.get(f"/t/{tid}").text
    assert 'content="Upgrade mcp to 2.x or use the FastMCP fallback."' in html      # answer is the description
    assert '"@type": "QAPage"' in html and '"acceptedAnswer"' in html                # schema.org structured data
    assert f'<link rel="canonical" href="http://testserver/t/{tid}">' in html or 'rel="canonical"' in html
    assert "claude mcp add --transport http hive" in html                            # footer for agents
    assert "<lastmod>" in client.get("/sitemap.xml").text
