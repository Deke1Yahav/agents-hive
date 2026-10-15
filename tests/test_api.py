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
    h2 = reg(client, "bot-b")
    assert client.post(f"/api/posts/{pid}/vote", json={"value": 1}, headers=h).status_code == 400  # own post
    assert client.post(f"/api/posts/{pid}/vote", json={"value": 1}, headers=h2).json()["votes"] == 1
    assert client.post(f"/api/posts/{pid}/vote", json={"value": 1}, headers=h2).json()["votes"] == 1  # idempotent
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


def test_dedup_needs_real_overlap(client):
    h = reg(client)
    assert client.post("/api/threads", json={"title": "How to use this forum efficiently", "body": "b"},
                       headers=h).status_code == 201
    # shares only stopwords ("to", "this") -> not a duplicate
    assert client.post("/api/threads", json={"title": "flyctl: no access token available in this shell", "body": "b"},
                       headers=h).status_code == 201
    # shares the key terms -> duplicate
    assert client.post("/api/threads", json={"title": "flyctl no access token available", "body": "b"},
                       headers=h).status_code == 409


def test_github_mirror_is_hardened(client, monkeypatch):
    import httpx
    import app as appmod
    calls = []

    class R:
        status_code = 201
        def json(self):
            return {"number": 77}

    monkeypatch.setattr(appmod, "GITHUB_REPO", "o/r")
    monkeypatch.setattr(appmod, "GITHUB_TOKEN", "t")
    monkeypatch.setattr(appmod, "MIRROR_PER_AGENT_DAY", 3)   # small cap so the test can hit it
    monkeypatch.setattr(httpx, "post", lambda url, **kw: calls.append(("post", url, kw)) or R())
    monkeypatch.setattr(httpx, "patch", lambda url, **kw: calls.append(("patch", url, kw)) or R())

    class SyncThread:  # run the app's background mirror threads inline so the test is deterministic
        def __init__(self, target, args=(), daemon=None):
            self.target, self.args = target, args
        def start(self):
            self.target(*self.args)
    monkeypatch.setattr(appmod.threading, "Thread", SyncThread)
    h = reg(client)
    tid = client.post("/api/threads", json={"title": "evil @octocat  click https://x.y", "body": "hi @octocat",
                                            "tags": ["spam-label"]}, headers=h).json()["id"]
    appmod.mirror_to_github(tid)          # no canonical yet -> nothing
    assert calls == []
    client.post(f"/api/threads/{tid}/canonical", json={"text": "see [link](https://x.y) @octocat"}, headers=h)
    kind, url, kw = calls[-1]
    assert kind == "post" and url.endswith("/repos/o/r/issues")
    assert kw["json"]["labels"] == ["hive"]                                  # tags never become labels
    assert "```text\nsee [link](https://x.y) @octocat\n```" in kw["json"]["body"]  # user text is fenced
    assert "```text\nhi @octocat\n```" in kw["json"]["body"]
    # second time -> update, never a second issue
    appmod.mirror_to_github(tid)
    assert calls[-1][0] == "patch" and calls[-1][1].endswith("/issues/77")
    # hidden by admin -> issue closed and blanked
    monkeypatch.setattr(appmod, "ADMIN_KEY", "adm")
    appmod.retract_from_github(tid)
    assert calls[-1][2]["json"] == {"title": "[removed]", "body": "Removed from Hive by moderation.",
                                    "state": "closed", "state_reason": "not_planned"}
    # per-agent cap: a 4th new issue from the same agent is not posted
    n = len(calls)
    for i in range(4):
        t2 = client.post("/api/threads", json={"title": f"q{i} totally different words {i}", "body": "b",
                                               "force": True}, headers=h).json()["id"]
        client.post(f"/api/threads/{t2}/canonical", json={"text": "a"}, headers=h)
    assert len([c for c in calls[n:] if c[0] == "post"]) == 2            # 3/day per agent, 1 already used


def test_indexnow_submit(client, monkeypatch):
    import httpx
    import app as appmod
    sent = []

    class R:
        status_code = 200
    monkeypatch.setattr(appmod, "INDEXNOW_KEY", "abc123")
    monkeypatch.setattr(appmod, "ADMIN_KEY", "adm")
    monkeypatch.setattr(httpx, "post", lambda url, **kw: sent.append((url, kw["json"])) or R())
    h = reg(client)
    client.post("/api/threads", json={"title": "q one", "body": "b"}, headers=h)
    r = client.post("/api/admin/indexnow", headers={"X-API-Key": "adm"}).json()
    assert r == {"submitted": 3, "status": 200}
    url, body = sent[-1]
    assert url == "https://api.indexnow.org/indexnow" and body["key"] == "abc123"
    assert body["keyLocation"].endswith("/abc123.txt") and body["urlList"][-1].endswith("/t/1")


def test_pinned_thread_first(client, monkeypatch):
    import app as appmod
    monkeypatch.setattr(appmod, "ADMIN_KEY", "adm")
    h = reg(client)
    first = client.post("/api/threads", json={"title": "welcome howto", "body": "b"}, headers=h).json()["id"]
    client.post("/api/threads", json={"title": "newer unrelated question", "body": "b"}, headers=h)
    assert client.get("/api/threads").json()["threads"][0]["id"] != first
    assert client.post(f"/api/admin/threads/{first}/pinned", headers={"X-API-Key": "adm"}).status_code == 200
    top = client.get("/api/threads").json()["threads"][0]
    assert top["id"] == first and top["pinned"] is True
    assert client.get("/").text.index("welcome howto") < client.get("/").text.index("newer unrelated")


def test_reputation(client, monkeypatch):
    import app as appmod
    monkeypatch.setattr(appmod, "REP_VOTER_MIN_AGE", 0)
    asker, helper, user = reg(client, "asker"), reg(client, "helper"), reg(client, "user")
    tid = client.post("/api/threads", json={"title": "ValueError: zero-size array to reduction", "body": "b"},
                      headers=asker).json()["id"]
    # no canonical yet -> cannot confirm
    assert client.post(f"/api/threads/{tid}/solved", headers=user).status_code == 400
    client.post(f"/api/threads/{tid}/canonical", json={"text": "check for empty input first"}, headers=helper)
    # the one who pinned it cannot confirm it; the asker and others can, once each
    assert client.post(f"/api/threads/{tid}/solved", headers=helper).status_code == 400
    assert client.post(f"/api/threads/{tid}/solved", headers=user).json() == {"confirmed": 1, "new": True}
    assert client.post(f"/api/threads/{tid}/solved", headers=user).json() == {"confirmed": 1, "new": False}
    assert client.post(f"/api/threads/{tid}/solved", headers=asker).json()["confirmed"] == 2
    me = lambda h: client.get("/api/me", headers=h).json()
    assert me(helper)["rep"] == 20 and me(asker)["rep"] == 2 and me(user)["rep"] == 0  # asker's own mark: no +2
    assert client.get(f"/api/threads/{tid}").json()["confirmed"] == 2
    assert client.get("/api/search", params={"q": "zero-size array"}).json()["hits"][0]["confirmed"] == 2
    assert "confirmed by 2 agents" in client.get(f"/t/{tid}").text
    # votes: +5 each, but one voter can move one agent by at most +30
    for i in range(8):
        pid = client.post(f"/api/threads/{tid}/posts", json={"body": f"detail {i}"}, headers=asker).json()["id"]
        client.post(f"/api/posts/{pid}/vote", json={"value": 1}, headers=user)
    assert me(asker)["rep"] == 30  # user's +2 solved mark and 8 x +5 votes share one cap of 30
    # leaderboard and profile
    board = client.get("/api/leaderboard").json()["agents"]
    assert [a["handle"] for a in board] == ["asker", "helper"]
    prof = client.get("/api/agents/helper").json()
    assert prof["rep"] == 20 and prof["answers_pinned"] == 1 and prof["answers_confirmed"] == 2
    assert "helper" in client.get("/leaderboard").text
    # levels raise the write limit
    assert appmod.level_of(0)["writes_per_hour"] == 30 and appmod.level_of(50)["level"] == "contributor"
    assert me(asker)["next_level"] == "contributor" and me(asker)["next_at"] == 50
    # replacing the answer resets confirmations; banning removes earned reputation
    client.post(f"/api/threads/{tid}/canonical", json={"text": "different fix"}, headers=asker)
    assert client.get(f"/api/threads/{tid}").json()["confirmed"] == 0
    client.post("/api/admin/agents/asker/banned", headers={"X-API-Key": "admin-secret"})
    assert [a["handle"] for a in client.get("/api/leaderboard").json()["agents"]] == []


def test_reputation_ignores_fresh_voters(client):
    a, b = reg(client, "old-agent"), reg(client, "fresh-agent")
    tid = client.post("/api/threads", json={"title": "fresh voter test", "body": "b"}, headers=a).json()["id"]
    client.post(f"/api/threads/{tid}/canonical", json={"text": "x"}, headers=a)
    assert client.post(f"/api/threads/{tid}/solved", headers=b).json()["confirmed"] == 1  # counted as a signal
    assert client.get("/api/me", headers=a).json()["rep"] == 0                            # but earns nothing yet


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
