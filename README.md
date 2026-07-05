# hermes-gnosis

A [hermes-agent](https://github.com/NousResearch/hermes-agent) memory-provider
plugin backed by a self-hosted [**gnosis**](https://github.com/bromigos-org/gnosis)
memory service. Point a hermes agent at your gnosis instance and it gains
durable, cross-session memory — recall injected into context automatically, plus
tools the model can call — with no changes to the agent itself.

It implements hermes's `MemoryProvider` ABC (`agent/memory_provider.py`) and
mirrors the shape of the bundled mem0 plugin, so activation is a one-liner.

## How it works

The plugin drives gnosis's `/v1/memories` surface across a turn:

1. **On session start** — `system_prompt_block()` injects a header telling the
   model it *has* persistent memory of this user and must call `gnosis_search`
   before answering anything context-dependent, followed by the user's top ~5
   stored memories. Those are fetched in a background thread on `initialize()`,
   so the prompt waits at most ~1 s for them and never blocks on a cold service.

2. **Before each turn** — `on_turn_start` kicks off a background **prefetch** of
   the user's question. By default this calls `POST /v1/memory/context`, gnosis's
   full **read pipeline** (adaptive routing, read-time supersession, graph-QA
   fusion/traversal, hybrid BM25, Chain-of-Note, facts→verbatim expansion,
   abstention/sufficiency), and injects its already-prompt-shaped `sections` as a
   `## Gnosis Memory` block. Set `recall_mode` to `search` to fall back to raw
   vector search instead; if the context endpoint errors, the plugin degrades to
   raw search automatically so recall never hard-fails. When hermes calls
   `prefetch()` the result is usually already warm; otherwise it waits at most
   **1.5 s**, then injects the block (or nothing, if the service is slow —
   `gnosis_search` is still the model's backstop). `queue_prefetch()` warms
   recall for the *next* turn. (The `gnosis_search` tool always uses raw search —
   it needs per-memory ids for `update`/`delete`.)

3. **During the turn** — the model can call five tools (below) to search, list,
   store, correct, or forget memories itself.

4. **After the turn** — `sync_turn()` sends the `(user, assistant)` pair to
   gnosis with `infer=true` for **server-side fact extraction**, on a
   non-blocking daemon thread. This runs only for primary contexts (cron and
   subagent turns are skipped, so background jobs never corrupt the user's
   memory).

Everything that touches the network runs off the agent's hot path or under a
short bounded wait, and a **circuit breaker** (below) sheds load when gnosis is
down. A memory blip degrades recall to empty; it never stalls or crashes the
agent loop.

## Install

hermes discovers out-of-tree memory providers from `$HERMES_HOME/plugins/<name>/`
(`$HERMES_HOME` defaults to `~/.hermes`).

```bash
pip install git+https://github.com/bromigos-org/hermes-gnosis   # or a local checkout path
hermes-gnosis-install                                           # copies it into $HERMES_HOME/plugins/gnosis/
```

Or skip pip entirely and symlink the package directory:

```bash
ln -s /path/to/hermes-gnosis/hermes_gnosis ~/.hermes/plugins/gnosis
```

(`httpx` must be importable in the hermes venv — it already is; hermes depends on
it.) The package also declares a `hermes_agent.plugins` pip entry point for
forward compatibility, but today hermes activates memory ("exclusive") providers
only via the plugins-directory discovery path, so `hermes-gnosis-install` (or the
symlink) is required.

## Activate

```bash
hermes config set memory.provider gnosis
echo 'GNOSIS_SERVICE_TOKEN=<service token>' >> ~/.hermes/.env
```

Or interactively: `hermes memory setup`, then select `gnosis`. Equivalent
`config.yaml` snippet:

```yaml
memory:
  provider: gnosis
```

You also need `gnosis_url` set (see below) before the plugin can reach the
service.

## Configuration

**Secret** (environment / `$HERMES_HOME/.env`):

| Env var | Description |
|---------|-------------|
| `GNOSIS_SERVICE_TOKEN` | Bearer token for the gnosis service. Preferred over any plaintext `gnosis_token`; the token is never persisted to `gnosis.json`. |

**Behavioral settings** (`$HERMES_HOME/gnosis.json`, written by
`hermes memory setup` / `save_config()`):

| Key | Default | Description |
|-----|---------|-------------|
| `gnosis_url` | — (**required**) | Base URL of the gnosis service |
| `user_id` | `hermes-user` | Canonical user id (see [Identity](#scope--identity)) |
| `agent_id` | `hermes` | Agent identifier in the gnosis scope |
| `tenant_id` | `bromigos` | Gnosis tenant — **must match** the server's `GNOSIS_TENANT_ID` |
| `timeout` | `10` | Read/search request timeout (seconds) |
| `add_timeout` | `30` | Extraction-mode add timeout (seconds) |
| `recall_mode` | `context` | Source for per-turn injected recall: `context` (full gnosis read pipeline via `POST /v1/memory/context`) or `search` (raw vector search). The `gnosis_search` tool always uses raw search regardless. |

Matching `GNOSIS_URL` / `GNOSIS_USER_ID` / `GNOSIS_AGENT_ID` / `GNOSIS_TENANT_ID`
/ `GNOSIS_TIMEOUT` / `GNOSIS_ADD_TIMEOUT` / `GNOSIS_RECALL_MODE` env vars are read
as fallback defaults; `gnosis.json` overrides them (except the token, where the
env var always wins).

## Tools

The model sees five tools (`get_tool_schemas()`); their descriptions steer the
model toward good memory hygiene:

| Tool | Does | Model uses it… |
|---|---|---|
| `gnosis_search` | semantic search, ranked (`limit` default 10, max 50) | before answering anything that may depend on the user — often several times, varying wording, for multi-hop questions |
| `gnosis_list` | full unranked, paginated dump (`page_size` default 100, max 200) | for an overview/audit, or to browse when there's no specific query |
| `gnosis_add` | store a fact **verbatim** (`infer=false`, no extraction) | the moment the user states a lasting preference, correction, decision, or detail |
| `gnosis_update` | replace a memory's text by id | when a stored fact changed or was wrong — correct in place instead of duplicating |
| `gnosis_delete` | delete a memory by id | when a fact is obsolete or the user asks to forget it |

Ids for `update`/`delete` come from a prior `search`/`list` result.
`gnosis_update` and `gnosis_delete` require the server-side edit flag (see
[Gnosis-side requirements](#gnosis-side-requirements)).

## Scope & identity

Every request carries a gnosis scope object:

```json
{
  "tenant_id": "<tenant_id>",
  "space_id": "hermes",
  "agent_id": "<agent_id>",
  "session_id": "<hermes session id, or \"hermes\">",
  "user_id": "<user_id>",
  "visibility": "private_user"
}
```

`guild_id`/`channel_id` are deliberately omitted — gnosis's scope model rejects
empty strings for those optional fields.

**Recall spans sessions.** gnosis keys long-term recall by `tenant_id` +
`user_id`; `session_id` is stored as write provenance and does **not** partition
reads, so a new hermes session still recalls everything about the same user.
Each write is also tagged with `metadata.channel` (the gateway name — `cli`,
`telegram`, `discord`, …) so per-channel filtered views are possible server-side
without coupling identity to channel.

**`user_id` resolution** (per turn, mirroring the mem0 plugin), first match wins:

1. an operator-configured `GNOSIS_USER_ID` / `gnosis.json` `user_id` — the
   canonical principal across every gateway;
2. the gateway-native id passed by hermes (Telegram numeric id, Discord
   snowflake, …);
3. the `hermes-user` fallback (e.g. CLI with no auth).

The literal default `hermes-user` is treated as *unset* at step 1, so a
leftover setup-wizard default never silently buckets every gateway user together.

## Resilience

- **Circuit breaker** — after **5 consecutive failures** the plugin pauses all
  gnosis calls for **120 s**, logs a warning naming the unreachable URL, then
  retries. Tool calls made while it's open return a clear "temporarily
  unavailable" message to the model.
- **4xx don't trip it** — client errors (a bad/not-found id on
  search/list/update/delete) are the user's fault, not the service's, so they
  surface to the model without counting toward the breaker.
- **Off the hot path** — top-memory warmup, prefetch, and turn-sync all run on
  daemon threads; the agent loop only ever waits the short bounded prefetch/
  prompt windows. Turn-sync de-duplicates: a still-running sync is joined
  briefly and skipped rather than double-ingesting.

## Gnosis-side requirements

- A gnosis service reachable at `gnosis_url`, exposing the v1 API — `POST
  /v1/memories`, `POST /v1/memories/search`, `POST /v1/memory/context`, `POST
  /v1/memories/list`, `PATCH /v1/memories/{id}`, `DELETE /v1/memories/{id}` —
  authenticated with `Authorization: Bearer <token>`. (`/v1/memory/context`
  powers the default `recall_mode`; with `recall_mode=search` only the plain
  endpoints are needed.)
- A service token valid for the configured `tenant_id`.
- **`GNOSIS_MEMORY_EDIT_ENABLED=true` on the server** for `gnosis_update` /
  `gnosis_delete`. With it off the server returns `403` and the tools report
  `"memory editing is disabled on the gnosis server"` to the model instead of
  erroring; `search`/`list`/`add` are unaffected.

The sibling gnosis repo ships a [`compose.yaml`](https://github.com/bromigos-org/gnosis/blob/main/compose.yaml)
and a [getting-started guide](https://github.com/bromigos-org/gnosis/blob/main/docs/getting-started.md)
that stands up a service this plugin can talk to.

## Privacy

`sync_turn` sends each user/assistant turn's **plain text** to gnosis for
extraction. Tool-call payloads (the `messages` kwarg) are **not** sent — only the
user and assistant text. The service token lives in the environment and is never
written to `gnosis.json`.

## Limitations

- **One provider at a time** — hermes enforces a single external memory provider;
  activating gnosis deactivates any other (mem0, honcho, …).
- **No client-side rerank** — the gnosis search contract exposes no rerank
  parameter (mem0 does); ranking is whatever the server returns. gnosis can
  rerank server-side via its own config, transparent to this plugin.
- **Edit tools need the server flag** — with `GNOSIS_MEMORY_EDIT_ENABLED` off,
  only `search`/`list`/`add` work.

## Development

```bash
pip install -e ".[dev]"
pytest
```

Tests use `httpx.MockTransport`; no gnosis service or hermes checkout is required
(the plugin falls back to a local mirror of the `MemoryProvider` ABC when hermes
isn't importable — see `_compat.py`).
