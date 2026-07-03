"""GnosisClient unit tests against the v1 API contract (MockTransport)."""

from __future__ import annotations

import pytest

from hermes_gnosis._client import GnosisError, GnosisPermissionError

from conftest import RecordingApp, make_client

SCOPE = {
    "tenant_id": "bromigos",
    "space_id": "hermes",
    "agent_id": "hermes",
    "session_id": "sess-1",
    "user_id": "hermes-user",
    "visibility": "private_user",
    "guild_id": "",
    "channel_id": "",
}


def test_auth_header_sent():
    app = RecordingApp()
    client = make_client(app)
    client.list(SCOPE)
    assert app.requests[0].headers["Authorization"] == "Bearer test-token"


def test_add_with_messages_and_infer():
    app = RecordingApp(responses={
        ("POST", "/v1/memories"): (200, {"results": [
            {"memory_id": "m-1", "content": "likes tea", "event": "ADD"},
        ]}),
    })
    client = make_client(app)
    result = client.add(
        SCOPE,
        messages=[{"role": "user", "content": "hi"},
                  {"role": "assistant", "content": "hello"}],
        infer=True,
        metadata={"channel": "cli"},
    )
    request = app.requests[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/memories"
    body = app.body()
    assert body["scope"] == SCOPE
    assert body["messages"] == [{"role": "user", "content": "hi"},
                                {"role": "assistant", "content": "hello"}]
    assert body["infer"] is True
    assert body["metadata"] == {"channel": "cli"}
    assert "content" not in body
    assert result["results"][0]["memory_id"] == "m-1"


def test_add_with_content_no_infer():
    app = RecordingApp(responses={
        ("POST", "/v1/memories"): (200, {"results": [
            {"memory_id": "m-2", "content": "verbatim fact", "event": "ADD"},
        ]}),
    })
    client = make_client(app)
    client.add(SCOPE, content="verbatim fact", infer=False)
    body = app.body()
    assert body["content"] == "verbatim fact"
    assert body["infer"] is False
    assert "messages" not in body


def test_search():
    app = RecordingApp(responses={
        ("POST", "/v1/memories/search"): (200, {"results": [
            {"memory_id": "m-1", "content": "likes tea", "score": 0.92,
             "metadata": {"channel": "cli"},
             "created_at": "2026-07-01T00:00:00Z",
             "updated_at": "2026-07-01T00:00:00Z"},
        ]}),
    })
    client = make_client(app)
    results = client.search(SCOPE, "beverages", limit=5)
    body = app.body()
    assert body == {"scope": SCOPE, "query": "beverages", "limit": 5}
    assert results[0]["memory_id"] == "m-1"
    assert results[0]["score"] == 0.92


def test_list():
    app = RecordingApp(responses={
        ("POST", "/v1/memories/list"): (200, {
            "results": [{"memory_id": "m-1", "content": "likes tea"}],
            "total": 1, "page": 2, "page_size": 50,
        }),
    })
    client = make_client(app)
    response = client.list(SCOPE, page=2, page_size=50)
    body = app.body()
    assert body == {"scope": SCOPE, "page": 2, "page_size": 50}
    assert response["total"] == 1
    assert response["page"] == 2
    assert response["results"][0]["memory_id"] == "m-1"


def test_update():
    app = RecordingApp(default_body={})
    client = make_client(app)
    client.update(SCOPE, "m-1", "prefers coffee")
    request = app.requests[0]
    assert request.method == "PATCH"
    assert request.url.path == "/v1/memories/m-1"
    assert app.body() == {"scope": SCOPE, "content": "prefers coffee"}


def test_delete():
    app = RecordingApp(default_body={})
    client = make_client(app)
    client.delete(SCOPE, "m-1")
    request = app.requests[0]
    assert request.method == "DELETE"
    assert request.url.path == "/v1/memories/m-1"
    assert app.body() == {"scope": SCOPE}


def test_403_raises_permission_error():
    app = RecordingApp(default_status=403, default_body={"error": "editing disabled"})
    client = make_client(app)
    with pytest.raises(GnosisPermissionError):
        client.update(SCOPE, "m-1", "x")
    with pytest.raises(GnosisPermissionError):
        client.delete(SCOPE, "m-1")


def test_5xx_raises_gnosis_error_with_status():
    app = RecordingApp(default_status=502, default_body={"error": "bad gateway"})
    client = make_client(app)
    with pytest.raises(GnosisError) as excinfo:
        client.search(SCOPE, "anything")
    assert excinfo.value.status_code == 502


def test_transport_error_wrapped():
    import httpx

    def _raise(request):
        raise httpx.ConnectError("connection refused", request=request)

    from hermes_gnosis._client import GnosisClient
    client = GnosisClient(
        "https://gnosis.test", "t", transport=httpx.MockTransport(_raise),
    )
    with pytest.raises(GnosisError):
        client.list(SCOPE)
