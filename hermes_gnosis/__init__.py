"""Gnosis memory plugin — MemoryProvider interface for hermes-agent.

Connects hermes-agent to a self-hosted gnosis memory service: server-side
fact extraction (``sync_turn`` with ``infer=true``), semantic search, and
verbatim fact storage, scoped per tenant/space/agent/user.

Configuration
-------------
Secret (lives in $HERMES_HOME/.env or the environment):
  GNOSIS_SERVICE_TOKEN — bearer token for the gnosis service (preferred over
                         any plaintext ``gnosis_token`` in gnosis.json)

Behavioral settings (live in $HERMES_HOME/gnosis.json, set via
``hermes memory setup``):
  gnosis_url  — base URL of the gnosis service (required)
  user_id     — canonical user identifier (default: "hermes-user"; when left
                at the default, gateway-native ids flow through instead)
  agent_id    — agent identifier (default: "hermes")
  tenant_id   — gnosis tenant (default: "bromigos")
  timeout     — read/search request timeout in seconds (default: 10)
  add_timeout — extraction-mode add timeout in seconds (default: 30)

Matching GNOSIS_URL / GNOSIS_USER_ID / GNOSIS_AGENT_ID / GNOSIS_TENANT_ID /
GNOSIS_TIMEOUT / GNOSIS_ADD_TIMEOUT env vars are read as fallback defaults;
gnosis.json overrides them (except the token, where the env var wins).
"""

from __future__ import annotations

import atexit
import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from ._compat import MemoryProvider, tool_error
from ._config import (
    DEFAULT_AGENT_ID,
    DEFAULT_SPACE_ID,
    DEFAULT_TENANT_ID,
    DEFAULT_USER_ID,
    TOKEN_ENV_VAR,
    load_config,
    save_config_file,
)
from ._client import GnosisClient, GnosisError, GnosisPermissionError

logger = logging.getLogger(__name__)

__version__ = "0.1.0"

# Circuit breaker: after this many consecutive failures, pause API calls
# for _BREAKER_COOLDOWN_SECS to avoid hammering a down server.
_BREAKER_THRESHOLD = 5
_BREAKER_COOLDOWN_SECS = 120
# How long the prefetch hot path waits for an in-flight background search.
_PREFETCH_WAIT_SECS = 1.5
# How long system_prompt_block() waits for the startup top-memories fetch.
_TOP_MEMORIES_WAIT_SECS = 1.0
_TOP_MEMORIES_COUNT = 5

_EDIT_DISABLED_MSG = "memory editing is disabled on the gnosis server"

# The default session_id used in the gnosis scope when hermes has not
# provided one.
_FALLBACK_SESSION_ID = "hermes"


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

LIST_SCHEMA = {
    "name": "gnosis_list",
    "description": (
        "List ALL stored memories about the user, unranked and paginated. "
        "Use for a full overview/audit at conversation start, or to browse "
        "everything when you don't have a specific query. For answering a "
        "specific question, prefer gnosis_search."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "page": {"type": "integer", "description": "Page number (default: 1)."},
            "page_size": {"type": "integer", "description": "Results per page (default: 100, max: 200)."},
        },
        "required": [],
    },
}

SEARCH_SCHEMA = {
    "name": "gnosis_search",
    "description": (
        "Search the user's memories by meaning; returns facts ranked by "
        "relevance. Use this BEFORE answering any question that may depend on "
        "what you know about the user (preferences, facts, history, people, "
        "projects, past decisions). For multi-part or multi-hop questions, "
        "call it MULTIPLE times — vary the wording and run follow-up searches "
        "on what earlier results reveal; one search is rarely enough."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
            "limit": {"type": "integer", "description": "Max results (default: 10, max: 50)."},
        },
        "required": ["query"],
    },
}

ADD_SCHEMA = {
    "name": "gnosis_add",
    "description": (
        "Store a durable fact about the user, verbatim (no LLM extraction). "
        "Call this the moment the user states a lasting preference, correction, "
        "decision, or personal detail worth recalling on future turns — don't "
        "wait to be asked to remember. Skip transient chit-chat and facts you've "
        "already stored."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The fact to store."},
        },
        "required": ["content"],
    },
}

UPDATE_SCHEMA = {
    "name": "gnosis_update",
    "description": (
        "Replace the text of an existing memory by its ID (take the ID from a "
        "gnosis_search or gnosis_list result). Use when a stored fact has "
        "changed or was wrong — correct it in place instead of adding a "
        "duplicate."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "Memory ID to update."},
            "content": {"type": "string", "description": "New text content."},
        },
        "required": ["memory_id", "content"],
    },
}

DELETE_SCHEMA = {
    "name": "gnosis_delete",
    "description": (
        "Delete a memory by its ID (take the ID from a gnosis_search or "
        "gnosis_list result). Use when a stored fact is obsolete or the user "
        "asks you to forget it; prefer gnosis_update if the fact merely "
        "changed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "Memory ID to delete."},
        },
        "required": ["memory_id"],
    },
}


# ---------------------------------------------------------------------------
# MemoryProvider implementation
# ---------------------------------------------------------------------------

class GnosisMemoryProvider(MemoryProvider):
    """Gnosis memory: self-hosted extraction, semantic search, scoped storage."""

    def __init__(self):
        self._config: Dict[str, Any] = {}
        self._client: Optional[GnosisClient] = None
        self._init_error = ""
        self._user_id = DEFAULT_USER_ID
        self._agent_id = DEFAULT_AGENT_ID
        self._tenant_id = DEFAULT_TENANT_ID
        self._session_id = ""
        self._channel = "cli"  # gateway channel name (cli/telegram/discord/...)
        self._agent_context = "primary"
        # Background threads
        self._sync_thread: Optional[threading.Thread] = None
        self._prefetch_thread: Optional[threading.Thread] = None
        self._top_memories_thread: Optional[threading.Thread] = None
        self._top_memories: List[str] = []
        self._prefetch_query = ""
        self._prefetch_result = ""
        self._prefetch_done = False
        # Circuit breaker state
        self._consecutive_failures = 0
        self._breaker_open_until = 0.0
        self._breaker_lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._prefetch_lock = threading.Lock()
        self._atexit_registered = False

    # -- Identity / availability ----------------------------------------------

    @property
    def name(self) -> str:
        return "gnosis"

    def is_available(self) -> bool:
        """Config check only — no network calls (per the ABC contract)."""
        cfg = load_config()
        return bool(cfg.get("gnosis_url")) and bool(cfg.get("gnosis_token"))

    # -- Config ----------------------------------------------------------------

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {"key": "gnosis_url", "description": "Gnosis service base URL (e.g. https://gnosis.example.com)", "required": True},
            {"key": "gnosis_token", "description": "Gnosis service bearer token", "secret": True, "required": True, "env_var": TOKEN_ENV_VAR},
            {"key": "user_id", "description": "User identifier", "default": DEFAULT_USER_ID},
            {"key": "agent_id", "description": "Agent identifier", "default": DEFAULT_AGENT_ID},
            {"key": "tenant_id", "description": "Gnosis tenant identifier", "default": DEFAULT_TENANT_ID},
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        """Write non-secret config to $HERMES_HOME/gnosis.json."""
        save_config_file(values, hermes_home)

    # -- Lifecycle ---------------------------------------------------------------

    def initialize(self, session_id: str, **kwargs) -> None:
        self._config = load_config()
        self._session_id = session_id or ""
        # Resolution order for user_id (mirrors the mem0 plugin):
        #   1. Operator-configured GNOSIS_USER_ID / gnosis.json user_id — the
        #      canonical principal across every gateway.
        #   2. Gateway-native id from kwargs (Telegram numeric id, Discord
        #      snowflake, ...).
        #   3. Hardcoded fallback DEFAULT_USER_ID (CLI with no auth).
        # The literal DEFAULT_USER_ID is treated as unset so setup-wizard
        # defaults don't silently bucket all gateway users together.
        configured = self._config.get("user_id")
        if configured == DEFAULT_USER_ID:
            configured = None
        self._user_id = configured or kwargs.get("user_id") or DEFAULT_USER_ID
        self._agent_id = self._config.get("agent_id", DEFAULT_AGENT_ID)
        self._tenant_id = self._config.get("tenant_id", DEFAULT_TENANT_ID)
        self._channel = kwargs.get("platform") or "cli"
        # Skip writes for non-primary contexts (cron system prompts would
        # corrupt user representations — see the ABC docstring).
        self._agent_context = kwargs.get("agent_context") or "primary"
        self._client = self._create_client()
        if self._client and not self._atexit_registered:
            atexit.register(self._shutdown_client)
            self._atexit_registered = True
        if self._client:
            self._start_top_memories_fetch()

    def _create_client(self) -> Optional[GnosisClient]:
        try:
            return GnosisClient(
                self._config.get("gnosis_url", ""),
                self._config.get("gnosis_token", ""),
                timeout=float(self._config.get("timeout", 10.0)),
                add_timeout=float(self._config.get("add_timeout", 30.0)),
            )
        except Exception as e:
            logger.error("Gnosis client failed to initialize: %s", e)
            self._init_error = str(e)
            return None

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs,
    ) -> None:
        self._session_id = new_session_id or ""
        if reset:
            with self._prefetch_lock:
                self._prefetch_query = ""
                self._prefetch_result = ""
                self._prefetch_done = False

    # -- Scope / metadata -------------------------------------------------------

    def _scope(self) -> Dict[str, Any]:
        """Build the gnosis scope object for the current session.

        guild_id/channel_id are omitted entirely: gnosis's scope model
        rejects empty strings (min_length=1) for optional fields.
        """
        return {
            "tenant_id": self._tenant_id,
            "space_id": DEFAULT_SPACE_ID,
            "agent_id": self._agent_id,
            "session_id": self._session_id or _FALLBACK_SESSION_ID,
            "user_id": self._user_id,
            "visibility": "private_user",
        }

    def _write_metadata(self) -> Dict[str, Any]:
        # Tag every write with the gateway channel so per-channel filtered
        # views are possible server-side without coupling identity to channel.
        return {"channel": self._channel} if self._channel else {}

    # -- Circuit breaker ---------------------------------------------------------

    def _is_breaker_open(self) -> bool:
        with self._breaker_lock:
            if self._consecutive_failures < _BREAKER_THRESHOLD:
                return False
            if time.monotonic() >= self._breaker_open_until:
                self._consecutive_failures = 0
                return False
            return True

    def _record_success(self) -> None:
        with self._breaker_lock:
            self._consecutive_failures = 0

    def _record_failure(self) -> None:
        with self._breaker_lock:
            self._consecutive_failures += 1
            count = self._consecutive_failures
            if count >= _BREAKER_THRESHOLD:
                self._breaker_open_until = time.monotonic() + _BREAKER_COOLDOWN_SECS
            else:
                count = 0
        if count >= _BREAKER_THRESHOLD:
            logger.warning(
                "Gnosis circuit breaker tripped after %d consecutive failures. "
                "Pausing API calls for %ds. Check that the gnosis service at "
                "%s is running and reachable.",
                count, _BREAKER_COOLDOWN_SECS, self._config.get("gnosis_url", "?"),
            )

    @staticmethod
    def _is_client_error(exc: Exception) -> bool:
        """User-caused errors (bad ID, not found) that shouldn't trip the breaker."""
        status = getattr(exc, "status_code", None)
        return status is not None and 400 <= status < 500

    # -- System prompt -------------------------------------------------------------

    def _start_top_memories_fetch(self) -> None:
        """Warm the top-memories cache for system_prompt_block (non-blocking)."""
        client = self._client
        if client is None:
            return

        def _run():
            try:
                response = client.list(
                    self._scope(), page=1, page_size=_TOP_MEMORIES_COUNT,
                )
                self._top_memories = [
                    m.get("content", "") for m in response.get("results", [])
                    if m.get("content")
                ]
                self._record_success()
            except Exception as e:
                self._record_failure()
                logger.debug("Gnosis top-memories fetch failed: %s", e)

        t = threading.Thread(target=_run, daemon=True, name="gnosis-top-memories")
        self._top_memories_thread = t
        t.start()

    def system_prompt_block(self) -> str:
        header = (
            "# Gnosis Memory\n"
            f"Active. User: {self._user_id}. Tenant: {self._tenant_id}.\n"
            "You have persistent memory of this user from past conversations. "
            "ALWAYS call gnosis_search before answering anything that could "
            "depend on prior context (the user's preferences, facts, history, "
            "people, projects, or earlier decisions) — do not rely on the chat "
            "window alone, and do not assume you have no memory.\n"
            "Tools: gnosis_search to find memories, gnosis_add to store facts, "
            "gnosis_list for a full overview, gnosis_update and gnosis_delete "
            "to manage by ID."
        )
        thread = self._top_memories_thread
        if thread and thread.is_alive():
            thread.join(timeout=_TOP_MEMORIES_WAIT_SECS)
        if self._top_memories:
            lines = "\n".join(f"- {m}" for m in self._top_memories)
            header += f"\nTop stored memories:\n{lines}"
        return header

    # -- Prefetch ---------------------------------------------------------------

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        self._start_prefetch(message)

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Queue a background recall for the NEXT turn. Never blocks, never raises."""
        try:
            self._start_prefetch(query)
        except Exception as e:  # belt-and-braces: never raise into the agent loop
            logger.debug("Gnosis queue_prefetch failed: %s", e)

    def _consume_prefetch_result(self, query: str) -> Optional[str]:
        with self._prefetch_lock:
            if self._prefetch_query != query or not self._prefetch_done:
                return None
            result = self._prefetch_result
            self._prefetch_result = ""
            self._prefetch_done = False
            return result

    def _start_prefetch(self, query: str) -> None:
        if not query or self._client is None or self._is_breaker_open():
            return
        client = self._client
        with self._prefetch_lock:
            if self._prefetch_query == query:
                if self._prefetch_done:
                    return
                if self._prefetch_thread and self._prefetch_thread.is_alive():
                    return
            self._prefetch_query = query
            self._prefetch_result = ""
            self._prefetch_done = False

        def _run():
            body = ""
            try:
                results = client.search(self._scope(), query, limit=10)
                lines = [r.get("content", "") for r in (results or [])
                         if r.get("content")]
                if lines:
                    body = "## Gnosis Memory\n" + "\n".join(f"- {l}" for l in lines)
                self._record_success()
            except Exception as e:
                self._record_failure()
                logger.debug("Gnosis prefetch failed: %s", e)
            with self._prefetch_lock:
                if self._prefetch_query == query:
                    self._prefetch_result = body
                    self._prefetch_done = True

        t = threading.Thread(target=_run, daemon=True, name="gnosis-prefetch")
        with self._prefetch_lock:
            self._prefetch_thread = t
        t.start()

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall memories for the CURRENT question with a short hot-path wait."""
        try:
            cached = self._consume_prefetch_result(query)
            if cached is not None:
                return cached
            self._start_prefetch(query)
            with self._prefetch_lock:
                thread = self._prefetch_thread if self._prefetch_query == query else None
            if thread:
                thread.join(timeout=_PREFETCH_WAIT_SECS)
            cached = self._consume_prefetch_result(query)
            if cached is not None:
                return cached
        except Exception as e:  # never raise into the agent loop
            logger.warning("Gnosis prefetch failed: %s", e)
        # Slow/down backend: skip injection; gnosis_search remains the backstop.
        return ""

    # -- Turn sync ------------------------------------------------------------------

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Send the turn to gnosis for server-side fact extraction (non-blocking)."""
        if self._client is None or self._is_breaker_open():
            return
        if self._agent_context not in ("", "primary"):
            # Don't extract facts from cron/subagent/flush contexts.
            return

        def _sync():
            client = self._client
            if client is None:
                return
            try:
                client.add(
                    self._scope(),
                    messages=[
                        {"role": "user", "content": user_content},
                        {"role": "assistant", "content": assistant_content},
                    ],
                    infer=True,
                    metadata=self._write_metadata(),
                )
                self._record_success()
            except Exception as e:
                self._record_failure()
                logger.warning("Gnosis sync failed: %s", e)

        with self._sync_lock:
            if self._sync_thread and self._sync_thread.is_alive():
                self._sync_thread.join(timeout=5.0)
            # If still alive after timeout, skip to avoid duplicate ingestion.
            if self._sync_thread and self._sync_thread.is_alive():
                return
            self._sync_thread = threading.Thread(
                target=_sync, daemon=True, name="gnosis-sync",
            )
            self._sync_thread.start()

    # -- Tools ------------------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [LIST_SCHEMA, SEARCH_SCHEMA, ADD_SCHEMA, UPDATE_SCHEMA, DELETE_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if self._client is None:
            err = self._init_error or "not configured"
            return json.dumps({"error": (
                f"Gnosis backend not initialized: {err}. "
                f"Set gnosis_url in gnosis.json and {TOKEN_ENV_VAR} in the environment."
            )})

        if self._is_breaker_open():
            return json.dumps({"error": (
                "Gnosis temporarily unavailable (multiple consecutive failures). "
                "Will retry automatically. Check that the gnosis service is running."
            )})

        if tool_name == "gnosis_list":
            return self._tool_list(args)
        if tool_name == "gnosis_search":
            return self._tool_search(args)
        if tool_name == "gnosis_add":
            return self._tool_add(args)
        if tool_name == "gnosis_update":
            return self._tool_update(args)
        if tool_name == "gnosis_delete":
            return self._tool_delete(args)
        return tool_error(f"Unknown tool: {tool_name}")

    def _tool_list(self, args: Dict[str, Any]) -> str:
        try:
            page = max(1, int(args.get("page", 1)))
            page_size = min(max(1, int(args.get("page_size", 100))), 200)
            response = self._client.list(self._scope(), page=page, page_size=page_size)
            self._record_success()
            results = response.get("results", [])
            if not results:
                return json.dumps({"result": "No memories stored yet."})
            items = [{"id": m.get("memory_id"), "memory": m.get("content", "")}
                     for m in results]
            return json.dumps({
                "results": items,
                "total": response.get("total", len(items)),
                "page": response.get("page", page),
                "page_size": response.get("page_size", page_size),
            })
        except Exception as e:
            if not self._is_client_error(e):
                self._record_failure()
            return tool_error(f"Failed to list memories: {e}")

    def _tool_search(self, args: Dict[str, Any]) -> str:
        query = args.get("query", "")
        if not query:
            return tool_error("Missing required parameter: query")
        try:
            limit = max(1, min(int(args.get("limit", 10)), 50))
            results = self._client.search(self._scope(), query, limit=limit)
            self._record_success()
            if not results:
                return json.dumps({"result": "No relevant memories found."})
            items = [{"id": r.get("memory_id"), "memory": r.get("content", ""),
                      "score": r.get("score", 0)} for r in results]
            return json.dumps({"results": items, "count": len(items)})
        except Exception as e:
            if not self._is_client_error(e):
                self._record_failure()
            return tool_error(f"Search failed: {e}")

    def _tool_add(self, args: Dict[str, Any]) -> str:
        content = args.get("content", "")
        if not content:
            return tool_error("Missing required parameter: content")
        try:
            response = self._client.add(
                self._scope(),
                content=content,
                infer=False,
                metadata=self._write_metadata(),
            )
            self._record_success()
            results = response.get("results", [])
            memory_id = results[0].get("memory_id") if results else None
            return json.dumps({"result": "Fact stored.", "memory_id": memory_id})
        except Exception as e:
            self._record_failure()
            return tool_error(f"Failed to store: {e}")

    def _tool_update(self, args: Dict[str, Any]) -> str:
        memory_id = args.get("memory_id", "")
        content = args.get("content", "")
        if not memory_id:
            return tool_error("Missing required parameter: memory_id")
        if not content:
            return tool_error("Missing required parameter: content")
        try:
            self._client.update(self._scope(), memory_id, content)
            self._record_success()
            return json.dumps({"result": "Memory updated.", "memory_id": memory_id})
        except GnosisPermissionError:
            return tool_error(_EDIT_DISABLED_MSG)
        except Exception as e:
            if self._is_client_error(e):
                return tool_error(f"Memory not found: {memory_id}")
            self._record_failure()
            return tool_error(f"Update failed: {e}")

    def _tool_delete(self, args: Dict[str, Any]) -> str:
        memory_id = args.get("memory_id", "")
        if not memory_id:
            return tool_error("Missing required parameter: memory_id")
        try:
            self._client.delete(self._scope(), memory_id)
            self._record_success()
            return json.dumps({"result": "Memory deleted.", "memory_id": memory_id})
        except GnosisPermissionError:
            return tool_error(_EDIT_DISABLED_MSG)
        except Exception as e:
            if self._is_client_error(e):
                return tool_error(f"Memory not found: {memory_id}")
            self._record_failure()
            return tool_error(f"Delete failed: {e}")

    # -- Shutdown --------------------------------------------------------------------

    def _shutdown_client(self) -> None:
        try:
            if self._client:
                self._client.close()
                self._client = None
        except Exception:
            pass

    def shutdown(self) -> None:
        for t in (self._prefetch_thread, self._sync_thread, self._top_memories_thread):
            if t and t.is_alive():
                t.join(timeout=5.0)
        self._shutdown_client()


def register(ctx) -> None:
    """Register gnosis as a memory provider plugin.

    Guarded with getattr so the same module also survives being loaded by
    hermes's general PluginManager (whose PluginContext has no
    register_memory_provider) — e.g. when installed via the pip entry point.
    """
    register_fn = getattr(ctx, "register_memory_provider", None)
    if callable(register_fn):
        register_fn(GnosisMemoryProvider())
