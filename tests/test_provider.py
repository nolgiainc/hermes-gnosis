"""GnosisMemoryProvider unit tests: tools, sync_turn, prefetch, scope."""

from __future__ import annotations

import json

from conftest import RecordingApp, make_provider


def _join_background(provider):
    for t in (provider._sync_thread, provider._prefetch_thread,
              provider._top_memories_thread):
        if t and t.is_alive():
            t.join(timeout=5.0)


# ---------------------------------------------------------------------------
# Scope construction
# ---------------------------------------------------------------------------

def test_scope_construction(app):
    provider = make_provider(app, session_id="sess-42", user_id="lesse")
    provider._agent_id = "hermes"
    provider._tenant_id = "nolgia"
    assert provider._scope() == {
        "tenant_id": "nolgia",
        "space_id": "hermes",
        "agent_id": "hermes",
        "session_id": "sess-42",
        "user_id": "lesse",
        "visibility": "private_user",
    }


def test_scope_session_fallback(app):
    provider = make_provider(app, session_id="")
    assert provider._scope()["session_id"] == "hermes"


def test_scope_tracks_session_switch(app):
    provider = make_provider(app, session_id="sess-1")
    provider.on_session_switch("sess-2", reset=True)
    assert provider._scope()["session_id"] == "sess-2"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def test_tool_list():
    app = RecordingApp(responses={
        ("POST", "/v1/memories/list"): (200, {
            "results": [{"memory_id": "m-1", "content": "likes tea"}],
            "total": 1, "page": 1, "page_size": 100,
        }),
    })
    provider = make_provider(app)
    result = json.loads(provider.handle_tool_call("gnosis_list", {}))
    assert result["results"] == [{"id": "m-1", "memory": "likes tea"}]
    assert result["total"] == 1
    assert app.body() == {"scope": provider._scope(), "page": 1, "page_size": 100}


def test_tool_list_empty():
    app = RecordingApp(responses={
        ("POST", "/v1/memories/list"): (200, {"results": [], "total": 0,
                                              "page": 1, "page_size": 100}),
    })
    provider = make_provider(app)
    result = json.loads(provider.handle_tool_call("gnosis_list", {}))
    assert result == {"result": "No memories stored yet."}


def test_tool_search():
    app = RecordingApp(responses={
        ("POST", "/v1/memories/search"): (200, {"results": [
            {"memory_id": "m-1", "content": "likes tea", "score": 0.9},
        ]}),
    })
    provider = make_provider(app)
    result = json.loads(provider.handle_tool_call(
        "gnosis_search", {"query": "beverages", "limit": 3},
    ))
    assert result["results"] == [{"id": "m-1", "memory": "likes tea", "score": 0.9}]
    assert app.body() == {"scope": provider._scope(), "query": "beverages", "limit": 3}


def test_tool_search_requires_query(app):
    provider = make_provider(app)
    result = json.loads(provider.handle_tool_call("gnosis_search", {}))
    assert "error" in result
    assert not app.requests


def test_tool_add_verbatim():
    app = RecordingApp(responses={
        ("POST", "/v1/memories"): (200, {"results": [
            {"memory_id": "m-9", "content": "likes tea", "event": "ADD"},
        ]}),
    })
    provider = make_provider(app, platform="discord")
    result = json.loads(provider.handle_tool_call(
        "gnosis_add", {"content": "likes tea"},
    ))
    assert result == {"result": "Fact stored.", "memory_id": "m-9"}
    body = app.body()
    assert body["content"] == "likes tea"
    assert body["infer"] is False
    assert "messages" not in body
    # Every write is tagged with the gateway channel.
    assert body["metadata"] == {"channel": "discord"}


def test_tool_update_and_delete():
    app = RecordingApp(default_body={})
    provider = make_provider(app)
    result = json.loads(provider.handle_tool_call(
        "gnosis_update", {"memory_id": "m-1", "content": "prefers coffee"},
    ))
    assert result == {"result": "Memory updated.", "memory_id": "m-1"}
    assert app.requests[0].method == "PATCH"
    assert app.requests[0].url.path == "/v1/memories/m-1"

    result = json.loads(provider.handle_tool_call(
        "gnosis_delete", {"memory_id": "m-1"},
    ))
    assert result == {"result": "Memory deleted.", "memory_id": "m-1"}
    assert app.requests[1].method == "DELETE"


def test_update_delete_403_surfaces_clear_message():
    app = RecordingApp(default_status=403, default_body={"error": "nope"})
    provider = make_provider(app)
    for tool, args in (
        ("gnosis_update", {"memory_id": "m-1", "content": "x"}),
        ("gnosis_delete", {"memory_id": "m-1"}),
    ):
        result = json.loads(provider.handle_tool_call(tool, args))
        assert result["error"] == "memory editing is disabled on the gnosis server"
    # Feature-flag 403s must not trip the circuit breaker.
    assert provider._consecutive_failures == 0


def test_tool_error_on_5xx_returns_json_not_exception():
    app = RecordingApp(default_status=500, default_body={"error": "boom"})
    provider = make_provider(app)
    result = json.loads(provider.handle_tool_call(
        "gnosis_search", {"query": "anything"},
    ))
    assert "error" in result


def test_unknown_tool(app):
    provider = make_provider(app)
    result = json.loads(provider.handle_tool_call("gnosis_bogus", {}))
    assert "error" in result


def test_uninitialized_client_returns_error():
    from hermes_gnosis import GnosisMemoryProvider
    provider = GnosisMemoryProvider()
    result = json.loads(provider.handle_tool_call("gnosis_search", {"query": "x"}))
    assert "not initialized" in result["error"]


def test_tool_schemas_expose_five_tools(app):
    provider = make_provider(app)
    names = [s["name"] for s in provider.get_tool_schemas()]
    assert names == ["gnosis_list", "gnosis_search", "gnosis_add",
                     "gnosis_update", "gnosis_delete"]


# ---------------------------------------------------------------------------
# sync_turn
# ---------------------------------------------------------------------------

def test_sync_turn_payload_shape():
    app = RecordingApp(responses={
        ("POST", "/v1/memories"): (200, {"results": []}),
    })
    provider = make_provider(app, platform="telegram")
    provider.sync_turn("what's my name?", "You're Lesse.", session_id="sess-1")
    _join_background(provider)
    assert len(app.requests) == 1
    body = app.body()
    assert body["scope"] == provider._scope()
    assert body["messages"] == [
        {"role": "user", "content": "what's my name?"},
        {"role": "assistant", "content": "You're Lesse."},
    ]
    assert body["infer"] is True
    assert body["metadata"] == {"channel": "telegram"}
    assert "content" not in body


def test_sync_turn_skipped_for_non_primary_context(app):
    provider = make_provider(app, agent_context="cron")
    provider.sync_turn("u", "a")
    _join_background(provider)
    assert not app.requests


def test_sync_turn_never_raises_on_error():
    app = RecordingApp(default_status=503, default_body={"error": "down"})
    provider = make_provider(app)
    provider.sync_turn("u", "a")  # must not raise
    _join_background(provider)
    assert provider._consecutive_failures >= 1


# ---------------------------------------------------------------------------
# prefetch / queue_prefetch
# ---------------------------------------------------------------------------

def test_prefetch_returns_formatted_memories():
    app = RecordingApp(responses={
        ("POST", "/v1/memories/search"): (200, {"results": [
            {"memory_id": "m-1", "content": "likes tea", "score": 0.9},
            {"memory_id": "m-2", "content": "works at nolgia", "score": 0.8},
        ]}),
    })
    provider = make_provider(app)
    # Pin to the raw-search recall path (the default is now "context").
    provider._recall_mode = "search"
    provider.queue_prefetch("what do I drink?")
    _join_background(provider)
    result = provider.prefetch("what do I drink?")
    assert "## Gnosis Memory" in result
    assert "- likes tea" in result
    assert "- works at nolgia" in result


def test_prefetch_context_mode_renders_sections():
    # Default recall_mode is "context": the read pipeline's sections are
    # concatenated verbatim (they're already prompt-shaped server-side).
    app = RecordingApp(responses={
        ("POST", "/v1/memory/context"): (200, {"sections": [
            {"source": "long_term_facts",
             "content": "- likes tea\n- works at nolgia", "facts": []},
            {"source": "graph", "content": "Collaborates with Bob.", "facts": []},
        ]}),
    })
    provider = make_provider(app)
    assert provider._recall_mode == "context"
    provider.queue_prefetch("what do I drink?")
    _join_background(provider)
    result = provider.prefetch("what do I drink?")
    assert result.startswith("## Gnosis Memory")
    assert "likes tea" in result
    assert "Collaborates with Bob." in result
    # Context mode hits the read pipeline, not raw vector search.
    assert app.requests[0].url.path == "/v1/memory/context"


def test_prefetch_context_mode_drops_short_term_sections():
    # short_term is dropped defensively — recall injection is for durable
    # cross-session memory, not the live conversation window.
    app = RecordingApp(responses={
        ("POST", "/v1/memory/context"): (200, {"sections": [
            {"source": "short_term", "content": "we're mid-conversation", "facts": []},
            {"source": "long_term_facts", "content": "likes tea", "facts": []},
        ]}),
    })
    provider = make_provider(app)
    provider.queue_prefetch("q")
    _join_background(provider)
    result = provider.prefetch("q")
    assert "likes tea" in result
    assert "mid-conversation" not in result


def test_prefetch_search_mode_uses_search_path():
    app = RecordingApp(responses={
        ("POST", "/v1/memories/search"): (200, {"results": [
            {"memory_id": "m-1", "content": "likes tea", "score": 0.9},
        ]}),
    })
    provider = make_provider(app)
    provider._recall_mode = "search"
    provider.queue_prefetch("what do I drink?")
    _join_background(provider)
    result = provider.prefetch("what do I drink?")
    assert "## Gnosis Memory" in result
    assert "- likes tea" in result
    # search mode never touches the context endpoint.
    assert app.requests[0].url.path == "/v1/memories/search"


def test_prefetch_context_failure_falls_back_to_search():
    # /v1/memory/context errors → degrade to raw search, still return a block.
    app = RecordingApp(responses={
        ("POST", "/v1/memory/context"): (500, {"error": "pipeline down"}),
        ("POST", "/v1/memories/search"): (200, {"results": [
            {"memory_id": "m-1", "content": "likes tea", "score": 0.9},
        ]}),
    })
    provider = make_provider(app)
    provider.queue_prefetch("what do I drink?")
    _join_background(provider)
    result = provider.prefetch("what do I drink?")
    assert "## Gnosis Memory" in result
    assert "- likes tea" in result
    paths = [r.url.path for r in app.requests]
    assert "/v1/memory/context" in paths
    assert "/v1/memories/search" in paths
    # A successful fallback is a success — the breaker stays closed.
    assert provider._consecutive_failures == 0


def test_prefetch_context_and_search_both_fail_returns_empty_once():
    # Both endpoints down → empty recall, no crash, and the breaker counts the
    # cycle exactly ONCE (context miss + search miss must not double-count).
    app = RecordingApp(default_status=500, default_body={"error": "down"})
    provider = make_provider(app)
    provider.queue_prefetch("anything")
    _join_background(provider)
    assert provider.prefetch("anything") == ""
    assert provider._consecutive_failures == 1


def test_prefetch_non_raising_on_server_error():
    app = RecordingApp(default_status=500, default_body={"error": "boom"})
    provider = make_provider(app)
    provider.queue_prefetch("anything")  # must not raise
    _join_background(provider)
    assert provider.prefetch("anything") == ""


def test_prefetch_non_raising_on_connection_error():
    import httpx

    def _raise(request):
        raise httpx.ConnectError("refused", request=request)

    app = RecordingApp()
    provider = make_provider(app)
    from hermes_gnosis._client import GnosisClient
    provider._client = GnosisClient(
        "https://gnosis.test", "t", transport=httpx.MockTransport(_raise),
    )
    provider.queue_prefetch("anything")
    _join_background(provider)
    assert provider.prefetch("anything") == ""


def test_prefetch_without_client_returns_empty():
    from hermes_gnosis import GnosisMemoryProvider
    provider = GnosisMemoryProvider()
    provider.queue_prefetch("x")
    assert provider.prefetch("x") == ""


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

def test_breaker_opens_after_consecutive_failures():
    app = RecordingApp(default_status=500, default_body={"error": "boom"})
    provider = make_provider(app)
    for _ in range(5):
        provider.handle_tool_call("gnosis_search", {"query": "x"})
    result = json.loads(provider.handle_tool_call("gnosis_search", {"query": "x"}))
    assert "temporarily unavailable" in result["error"]
    # Breaker open — the 6th call must not hit the network.
    assert len(app.requests) == 5


# ---------------------------------------------------------------------------
# system_prompt_block
# ---------------------------------------------------------------------------

def test_system_prompt_block_includes_header_and_top_memories():
    app = RecordingApp(responses={
        ("POST", "/v1/memories/list"): (200, {
            "results": [{"memory_id": "m-1", "content": "likes tea"}],
            "total": 1, "page": 1, "page_size": 5,
        }),
    })
    provider = make_provider(app)
    provider._start_top_memories_fetch()
    _join_background(provider)
    block = provider.system_prompt_block()
    assert block.startswith("# Gnosis Memory")
    assert "gnosis_search" in block
    assert "- likes tea" in block


def test_system_prompt_block_degrades_without_backend():
    app = RecordingApp(default_status=500, default_body={"error": "boom"})
    provider = make_provider(app)
    provider._start_top_memories_fetch()
    _join_background(provider)
    block = provider.system_prompt_block()
    assert block.startswith("# Gnosis Memory")
    assert "Top stored memories" not in block
