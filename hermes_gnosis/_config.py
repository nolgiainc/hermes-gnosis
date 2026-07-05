"""Config handling for the gnosis memory provider.

Mirrors the mem0 plugin's config pattern:

- Secrets live in the environment (``$HERMES_HOME/.env`` is loaded by hermes):
    GNOSIS_SERVICE_TOKEN — bearer token for the gnosis service (preferred
                           over any plaintext ``gnosis_token`` in gnosis.json)

- Behavioral settings live in ``$HERMES_HOME/gnosis.json`` (written by
  ``hermes memory setup`` via ``save_config()``):
    gnosis_url    — base URL of the gnosis service (e.g. https://gnosis.local)
    user_id       — canonical user identifier (default: "hermes-user")
    agent_id      — agent identifier (default: "hermes")
    tenant_id     — gnosis tenant (default: "bromigos")
    timeout       — request timeout in seconds for reads (default: 10)
    add_timeout   — request timeout for extraction-mode adds (default: 30)
    recall_mode   — source for per-turn injected recall: "context" (full
                    gnosis read pipeline via /v1/memory/context, default) or
                    "search" (raw vector search)

Matching GNOSIS_URL / GNOSIS_USER_ID / GNOSIS_AGENT_ID / GNOSIS_TENANT_ID /
GNOSIS_TIMEOUT / GNOSIS_ADD_TIMEOUT / GNOSIS_RECALL_MODE env vars are read as
defaults; gnosis.json overrides them (except the token, where the env var wins).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

CONFIG_FILENAME = "gnosis.json"

DEFAULT_USER_ID = "hermes-user"
DEFAULT_AGENT_ID = "hermes"
DEFAULT_TENANT_ID = "bromigos"
DEFAULT_SPACE_ID = "hermes"
DEFAULT_TIMEOUT = 10.0
DEFAULT_ADD_TIMEOUT = 30.0
# Per-turn injected recall source: "context" runs gnosis's full read pipeline
# (/v1/memory/context); "search" is the legacy raw vector top-k.
DEFAULT_RECALL_MODE = "context"

TOKEN_ENV_VAR = "GNOSIS_SERVICE_TOKEN"


def get_hermes_home() -> Path:
    """Resolve the active HERMES_HOME, inside or outside a hermes process."""
    try:  # pragma: no cover - hermes runtime only
        from hermes_constants import get_hermes_home as _ghh  # type: ignore
        return Path(_ghh())
    except ImportError:
        return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()


def _as_float(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def load_config() -> Dict[str, Any]:
    """Load config from env vars, with $HERMES_HOME/gnosis.json overrides.

    Environment variables provide defaults; gnosis.json (if present)
    overrides individual non-empty keys. The service token is special:
    GNOSIS_SERVICE_TOKEN in the environment always wins over a plaintext
    ``gnosis_token`` stored in gnosis.json.
    """
    config: Dict[str, Any] = {
        "gnosis_url": os.environ.get("GNOSIS_URL", ""),
        "gnosis_token": os.environ.get(TOKEN_ENV_VAR, ""),
        "agent_id": os.environ.get("GNOSIS_AGENT_ID", DEFAULT_AGENT_ID),
        "tenant_id": os.environ.get("GNOSIS_TENANT_ID", DEFAULT_TENANT_ID),
        "timeout": _as_float(os.environ.get("GNOSIS_TIMEOUT"), DEFAULT_TIMEOUT),
        "add_timeout": _as_float(os.environ.get("GNOSIS_ADD_TIMEOUT"), DEFAULT_ADD_TIMEOUT),
        "recall_mode": os.environ.get("GNOSIS_RECALL_MODE", DEFAULT_RECALL_MODE),
    }
    # Only carry user_id when the operator explicitly configured one, so
    # initialize() can fall back to the gateway-native id from kwargs
    # (same semantics as the mem0 plugin).
    env_user_id = os.environ.get("GNOSIS_USER_ID")
    if env_user_id:
        config["user_id"] = env_user_id

    config_path = get_hermes_home() / CONFIG_FILENAME
    if config_path.exists():
        try:
            file_cfg = json.loads(config_path.read_text(encoding="utf-8"))
            config.update({k: v for k, v in file_cfg.items()
                           if v is not None and v != ""})
        except Exception:
            pass

    # Env token is preferred over any plaintext token persisted in the file.
    env_token = os.environ.get(TOKEN_ENV_VAR)
    if env_token:
        config["gnosis_token"] = env_token

    config["timeout"] = _as_float(config.get("timeout"), DEFAULT_TIMEOUT)
    config["add_timeout"] = _as_float(config.get("add_timeout"), DEFAULT_ADD_TIMEOUT)
    return config


def save_config_file(values: Dict[str, Any], hermes_home: str) -> None:
    """Persist non-secret config to $HERMES_HOME/gnosis.json (merge-update)."""
    config_path = Path(hermes_home) / CONFIG_FILENAME
    existing: Dict[str, Any] = {}
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    existing.update(values)
    # Never persist the secret token to the JSON file.
    existing.pop("gnosis_token", None)
    try:  # pragma: no cover - hermes runtime only
        from utils import atomic_json_write  # type: ignore
        atomic_json_write(config_path, existing, mode=0o600)
    except ImportError:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        try:
            os.chmod(config_path, 0o600)
        except OSError:
            pass
