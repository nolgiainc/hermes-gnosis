# hermes-gnosis

`hermes-gnosis` is an out-of-tree [hermes-agent](https://github.com/NousResearch/hermes-agent)
memory provider backed by a self-hosted [gnosis](https://github.com/nolgiainc/gnosis)
service. It implements the Hermes `MemoryProvider` interface and exposes scoped
recall plus five model-callable memory tools. The plugin does not include a
Gnosis server or a Hermes checkout.

The package metadata and supported Python version are in [`pyproject.toml`](pyproject.toml).
The implementation is in [`hermes_gnosis/`](hermes_gnosis/), with request behavior
in [`_client.py`](hermes_gnosis/_client.py) and configuration loading in
[`_config.py`](hermes_gnosis/_config.py).

## Lifecycle

The provider follows the Hermes memory-provider callbacks. Network work is
backgrounded where the callback is intended to be best-effort, but model tool
calls are synchronous and can wait for the configured HTTP timeout.

1. **Startup.** `initialize()` creates the HTTP client and starts a daemon list
   request for page 1 with `page_size=5`. `system_prompt_block()` waits up to
   **1.0 second** for that request, then includes whatever first five memories
   are available. The list endpoint is unranked; this is not a relevance-ranked
   summary. The prompt header is returned even when the service is unavailable.
2. **Before a turn.** `on_turn_start()` starts a daemon prefetch for the user’s
   question. The default `recall_mode=context` calls
   `POST /v1/memory/context` and renders the returned long-term sections. A
   context error falls back to raw `POST /v1/memories/search`; `recall_mode=search`
   uses raw search directly. `prefetch()` waits up to **1.5 seconds** for the
   current question and otherwise injects no block. `queue_prefetch()` only warms
   the next query and does not wait. The `gnosis_search` tool always uses raw
   search so its results include IDs for later edits.
3. **During a turn.** Hermes invokes the five tools listed below on the model
   thread. They perform synchronous HTTP requests; the short prefetch wait does
   not apply to them.
4. **After a turn.** For a primary context only, `sync_turn()` starts a daemon
   `POST /v1/memories` request containing the user and assistant text with
   `infer=true`. It ignores the optional `messages` callback argument and does
   not send tool-call payloads. If an earlier sync is still running, the provider
   joins it for up to **5 seconds**; if it is still alive, the new sync is skipped
   to avoid duplicate ingestion. Cron and subagent contexts are skipped.

## Install and activate

Python **3.11 or newer** is required. Install from a checkout or directly from
the Git repository, then copy the provider into Hermes’ plugin directory:

```bash
python -m pip install git+https://github.com/nolgiainc/hermes-gnosis
hermes-gnosis-install
```

`hermes-gnosis-install` copies the package to
`$HERMES_HOME/plugins/gnosis/`. `$HERMES_HOME` defaults to `~/.hermes`; use a
separate profile with `--hermes-home`:

```bash
hermes-gnosis-install --hermes-home /path/to/hermes-home
```

For a source checkout, a symlink to the package directory is an alternative:

```bash
mkdir -p /path/to/hermes-home/plugins
ln -s /path/to/hermes-gnosis/hermes_gnosis \
  /path/to/hermes-home/plugins/gnosis
```

The installer only copies this provider. Hermes, `httpx`, a reachable Gnosis
service, and a valid service token remain prerequisites. The package declares a
`hermes_agent.plugins` entry point for forward compatibility, but the current
Hermes exclusive-memory discovery path is the
`$HERMES_HOME/plugins/<name>/` directory, so install or link the package there.

Activate the provider and supply the token through Hermes’ environment file:

```bash
hermes config set memory.provider gnosis
echo 'GNOSIS_SERVICE_TOKEN=<service-token>' >> "$HERMES_HOME/.env"
```

Then set `gnosis_url` in `gnosis.json` (or run `hermes memory setup`). A minimal
activation config is:

```yaml
memory:
  provider: gnosis
```

For example, the behavioral portion of `gnosis.json` can be written without a
token:

```json
{
  "gnosis_url": "<gnosis-base-url>",
  "tenant_id": "nolgia",
  "agent_id": "hermes",
  "recall_mode": "context"
}
```

## Configuration

The provider resolves its home directory from Hermes’ `get_hermes_home()` when
running inside Hermes. Outside Hermes it uses `$HERMES_HOME`, or `~/.hermes`
when that variable is unset. Behavioral settings are read from
`$HERMES_HOME/gnosis.json`; environment variables provide defaults.

| Setting | Default | Meaning |
| --- | --- | --- |
| `gnosis_url` / `GNOSIS_URL` | required | Gnosis base URL. |
| `user_id` / `GNOSIS_USER_ID` | unset, then `hermes-user` | Canonical user identifier. A configured literal `hermes-user` is treated as unset so a gateway-native ID can be used. |
| `agent_id` / `GNOSIS_AGENT_ID` | `hermes` | Agent scope component. |
| `tenant_id` / `GNOSIS_TENANT_ID` | `nolgia` | Gnosis tenant; it must match the server configuration. |
| `timeout` / `GNOSIS_TIMEOUT` | `10` seconds | Default `httpx` timeout for read/search/context/list/update/delete requests, including the startup list request. |
| `add_timeout` / `GNOSIS_ADD_TIMEOUT` | `30` seconds | Explicit timeout for every `POST /v1/memories` add, including verbatim `gnosis_add` and `sync_turn` extraction. |
| `recall_mode` / `GNOSIS_RECALL_MODE` | `context` | `context` uses the full read pipeline; `search` uses raw vector search. |

The token is `GNOSIS_SERVICE_TOKEN`. A non-empty token in the environment
always wins over a plaintext `gnosis_token` in `gnosis.json`; the latter is
accepted only as a compatibility fallback. `save_config()` strips
`gnosis_token` before writing the JSON file. Non-empty keys in `gnosis.json`
override environment defaults; malformed JSON is ignored, and invalid numeric
timeout values fall back to their defaults.

The plugin’s `get_config_schema()` exposes `gnosis_url`, the token, `user_id`,
`agent_id`, `tenant_id`, and `recall_mode` to `hermes memory setup`. It does not
expose `timeout` or `add_timeout` in that wizard schema; set those two values in
the JSON file or with their environment variables. `recall_mode` is not
validated by the loader: `context` selects the context endpoint and any other
value follows the raw-search branch. See [`_config.py`](hermes_gnosis/_config.py)
for the exact precedence and fallback code.

## Timeouts and bounded waits

The timeout values above are HTTP request timeouts, not global callback
deadlines. The provider adds these callback-level bounds:

| Operation | Bound |
| --- | --- |
| `system_prompt_block()` waiting for startup memories | 1.0s |
| `prefetch()` waiting for the current query | 1.5s |
| `sync_turn()` waiting for an older sync before deduplicating | 5.0s, then skip the new sync |
| Model tools | No extra provider-level bound; reads use `timeout`, adds use `add_timeout`. |

An empty prefetch result means recall injection was unavailable or no matching
long-term content was returned. The model can still call `gnosis_search`.

## Tools

The provider registers exactly five tools:

| Tool | Behavior |
| --- | --- |
| `gnosis_search` | Ranked semantic search; `limit` defaults to 10 and is capped at 50. |
| `gnosis_list` | Unranked paginated listing; `page_size` defaults to 100 and is capped at 200. |
| `gnosis_add` | Stores the supplied text verbatim with `infer=false`; writes are tagged with the gateway channel. |
| `gnosis_update` | Replaces a memory by ID from a prior search/list result. |
| `gnosis_delete` | Deletes a memory by ID from a prior search/list result. |

Update and delete require the Gnosis server’s edit feature flag. When
`GNOSIS_MEMORY_EDIT_ENABLED=true` is not enabled, the provider returns a clear
“memory editing is disabled on the gnosis server” tool error for those two
operations; search, list, and add are unaffected. Tool argument validation
(such as a missing query or content) returns an error without making a request.

## Scope and privacy

Every request carries this scope:

```json
{
  "tenant_id": "<tenant_id>",
  "space_id": "hermes",
  "agent_id": "<agent_id>",
  "session_id": "<Hermes session id or hermes>",
  "user_id": "<user_id>",
  "visibility": "private_user"
}
```

`guild_id` and `channel_id` are intentionally omitted because Gnosis rejects
empty values for those optional fields. Long-term recall spans sessions for a
given `tenant_id` + `user_id`; `session_id` records write provenance rather than
partitioning reads. Writes include `metadata.channel` (for example `cli`,
`telegram`, or `discord`). User identity is resolved in this order:

1. An operator-configured `GNOSIS_USER_ID` or `gnosis.json` `user_id`.
2. The gateway-native ID passed to `initialize()`.
3. The `hermes-user` fallback.

`sync_turn()` sends only the plain user and assistant text to Gnosis for
server-side extraction. It does not send the `messages` callback argument or
tool-call payloads. The service token is read from the environment and is not
written to `gnosis.json` by the plugin.

## Circuit breaker

The provider maintains one consecutive-failure counter and opens its circuit
after **five consecutive counted failures**. Each operation decides whether its
failure contributes to that shared counter. While open, calls are skipped for
**120 seconds** and model tools return a temporary-unavailable error; after the
cooldown, the failure counter resets and calls may resume. A counted successful
request resets the counter as well.

The operation-specific rules are:

- `gnosis_list`, `gnosis_search`, `gnosis_update`, and `gnosis_delete` do not
  count HTTP 4xx errors as breaker failures. The 403 edit response is handled
  separately and never trips the breaker.
- `gnosis_add` counts any request exception, including an HTTP 4xx response.
- Startup top-memory fetches and `sync_turn()` count any request exception.
- Prefetch counts only its final outcome once. In `context` mode, a failed
  context request followed by a successful raw-search fallback is a success;
  if both fail, the cycle counts one failure.

The 4xx exemption is therefore operation-specific, not a blanket provider
guarantee.
The implementation is in the breaker and tool handlers in
[`__init__.py`](hermes_gnosis/__init__.py).

## Gnosis-side requirements

The configured service must expose these authenticated v1 endpoints:

- `POST /v1/memories` for verbatim adds and `infer=true` turn extraction;
- `POST /v1/memories/search` for raw search and the `gnosis_search` tool;
- `POST /v1/memory/context` for the default context recall mode;
- `POST /v1/memories/list` for startup and `gnosis_list`;
- `PATCH /v1/memories/{id}` and `DELETE /v1/memories/{id}` for edit tools.

Requests use `Authorization: Bearer <token>`. The token must be valid for the
configured tenant. The sibling Gnosis repository provides a tracked
[`compose.yaml`](https://github.com/nolgiainc/gnosis/blob/main/compose.yaml) and
[getting-started guide](https://github.com/nolgiainc/gnosis/blob/main/docs/getting-started.md)
for service setup.

## Development and compatibility boundary

Use a locked environment and keep build output outside the repository:

```bash
uv sync --extra dev --locked
uv run pytest -q
uv build --out-dir /tmp/hermes-gnosis-build
uv run hermes-gnosis-install --help
```

The tests use `httpx.MockTransport`; they do not contact a live Gnosis service
or exercise a real Hermes process. When Hermes is not importable, the provider
uses the local interface mirror in [`_compat.py`](hermes_gnosis/_compat.py).
The test suite therefore verifies the mocked HTTP contract, lifecycle callbacks,
configuration behavior, and tool handling—not end-to-end Hermes/Gnosis
integration. Test sources are in [`tests/`](tests/).

`dist/` is ignored by [`.gitignore`](.gitignore) and is not a release record.
Build wheels and source archives into a run-owned directory, as above; do not
infer a published package or upload status from a local ignored artifact.
