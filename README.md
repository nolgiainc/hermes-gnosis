# hermes-gnosis

A [hermes-agent](https://github.com/NousResearch/hermes-agent) memory provider
plugin backed by our self-hosted **gnosis** memory service.

It implements hermes's `MemoryProvider` ABC (`agent/memory_provider.py`) and
mirrors the shape of the bundled mem0 plugin:

- **Five model-facing tools**: `gnosis_list`, `gnosis_search`, `gnosis_add`
  (verbatim, `infer=false`), `gnosis_update`, `gnosis_delete`
- **System prompt block** — short header plus the top stored memories
- **Prefetch** — background semantic search before each turn (bounded 1.5s
  hot-path wait; never blocks the agent loop)
- **Turn sync** — after each turn, the `(user, assistant)` pair is sent to
  gnosis with `infer=true` for server-side fact extraction (non-blocking
  daemon thread; skipped for non-primary contexts such as cron/subagents)
- **Resilience** — network/5xx failures log a warning and degrade to empty
  results; a circuit breaker pauses API calls for 2 minutes after 5
  consecutive failures
- **Channel tagging** — every write carries `metadata.channel` with the
  gateway name (`cli`, `telegram`, `discord`, ...)

## Install

hermes discovers out-of-tree memory providers from `$HERMES_HOME/plugins/<name>/`.

From a local checkout or git:

```bash
pip install /path/to/hermes-gnosis          # or: pip install git+https://github.com/bromigos-org/hermes-gnosis
hermes-gnosis-install                        # copies the plugin into $HERMES_HOME/plugins/gnosis/
```

Or skip pip entirely and copy/symlink the package directory:

```bash
ln -s /path/to/hermes-gnosis/hermes_gnosis ~/.hermes/plugins/gnosis
```

(`httpx` must be importable in the hermes venv — it already is, hermes depends
on it.)

The package also declares a `hermes_agent.plugins` pip entry point for
forward compatibility, but today hermes activates memory ("exclusive")
providers only via the plugins-directory discovery path, so
`hermes-gnosis-install` (or the symlink) is required.

## Activate

```bash
hermes config set memory.provider gnosis
echo 'GNOSIS_SERVICE_TOKEN=<service token>' >> ~/.hermes/.env
```

Or interactively: `hermes memory setup` and select `gnosis`.

Equivalent `config.yaml` snippet:

```yaml
memory:
  provider: gnosis
```

## Configuration

Secret (environment / `$HERMES_HOME/.env`):

| Env var | Description |
|---------|-------------|
| `GNOSIS_SERVICE_TOKEN` | Bearer token for the gnosis service. Preferred over any plaintext `gnosis_token`; `save_config()` never persists the token to disk. |

Behavioral settings (`$HERMES_HOME/gnosis.json`, written by
`hermes memory setup` / `save_config()`):

| Key | Default | Description |
|-----|---------|-------------|
| `gnosis_url` | — (required) | Base URL of the gnosis service |
| `user_id` | `hermes-user` | Canonical user id. Left at the default, gateway-native ids (Telegram id, Discord snowflake, ...) flow through instead |
| `agent_id` | `hermes` | Agent identifier in the gnosis scope |
| `tenant_id` | `bromigos` | Gnosis tenant |
| `timeout` | `10` | Read/search request timeout (seconds) |
| `add_timeout` | `30` | Extraction-mode add timeout (seconds) |

Matching `GNOSIS_URL` / `GNOSIS_USER_ID` / `GNOSIS_AGENT_ID` /
`GNOSIS_TENANT_ID` / `GNOSIS_TIMEOUT` / `GNOSIS_ADD_TIMEOUT` env vars are read
as fallback defaults; `gnosis.json` overrides them (except the token, where
the env var wins).

## Gnosis-side requirements

- The gnosis memory service reachable at `gnosis_url`, exposing the v1 API:
  `POST /v1/memories`, `POST /v1/memories/search`, `POST /v1/memories/list`,
  `PATCH /v1/memories/{id}`, `DELETE /v1/memories/{id}`, authenticated with
  `Authorization: Bearer <token>`.
- A service token valid for the configured tenant.
- `GNOSIS_MEMORY_EDIT_ENABLED` on the **server** for `gnosis_update` /
  `gnosis_delete`. When editing is flagged off, the server returns 403 and
  the tools report `"memory editing is disabled on the gnosis server"` to the
  model instead of erroring.

All requests carry a scope object:

```json
{
  "tenant_id": "<tenant_id>",
  "space_id": "hermes",
  "agent_id": "<agent_id>",
  "session_id": "<hermes session id, or \"hermes\">",
  "user_id": "<user_id>",
  "visibility": "private_user",
  "guild_id": "",
  "channel_id": ""
}
```

## Limitations

- **Session-scoped reads**: the scope's `session_id` is the current hermes
  session; whether recall spans sessions is decided server-side by how gnosis
  interprets the scope (hermes's mem0 plugin reads across all sessions by
  user id — gnosis should treat `session_id` as provenance, not a read
  filter, to match).
- **One provider at a time**: hermes enforces a single external memory
  provider; activating gnosis deactivates any other (mem0, honcho, ...).
- **No rerank flag**: the gnosis search contract has no rerank parameter
  (mem0 exposes one); ranking quality is whatever the server returns.
- **update/delete need the server flag**: with `GNOSIS_MEMORY_EDIT_ENABLED`
  off, only list/search/add work.
- **Privacy**: `sync_turn` sends each user/assistant turn text to the gnosis
  service for extraction. Tool-call payloads (`messages` kwarg) are *not*
  sent — only the plain user and assistant text.

## Development

```bash
pip install -e ".[dev]"
pytest
```

Tests use `httpx.MockTransport`; no gnosis service or hermes checkout is
required (the plugin falls back to a local mirror of the `MemoryProvider`
ABC when hermes isn't importable).
