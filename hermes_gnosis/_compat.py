"""Compatibility shims so the plugin imports both inside and outside hermes.

Inside a hermes-agent process, ``agent.memory_provider.MemoryProvider`` and
``tools.registry.tool_error`` are importable. Outside (unit tests, linting,
pip build), we fall back to local stand-ins with identical semantics for the
subset of the interface this plugin uses.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

try:  # pragma: no cover - exercised only inside a hermes runtime
    from agent.memory_provider import MemoryProvider  # type: ignore
except ImportError:  # standalone: minimal mirror of the hermes ABC
    from abc import ABC, abstractmethod

    class MemoryProvider(ABC):  # type: ignore[no-redef]
        """Local mirror of hermes-agent's agent/memory_provider.py ABC."""

        @property
        @abstractmethod
        def name(self) -> str: ...

        @abstractmethod
        def is_available(self) -> bool: ...

        @abstractmethod
        def initialize(self, session_id: str, **kwargs) -> None: ...

        def system_prompt_block(self) -> str:
            return ""

        def prefetch(self, query: str, *, session_id: str = "") -> str:
            return ""

        def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
            pass

        def sync_turn(
            self,
            user_content: str,
            assistant_content: str,
            *,
            session_id: str = "",
            messages: Optional[List[Dict[str, Any]]] = None,
        ) -> None:
            pass

        @abstractmethod
        def get_tool_schemas(self) -> List[Dict[str, Any]]: ...

        def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
            raise NotImplementedError(
                f"Provider {self.name} does not handle tool {tool_name}"
            )

        def shutdown(self) -> None:
            pass

        def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
            pass

        def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
            pass

        def on_session_switch(
            self,
            new_session_id: str,
            *,
            parent_session_id: str = "",
            reset: bool = False,
            rewound: bool = False,
            **kwargs,
        ) -> None:
            pass

        def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
            return ""

        def on_delegation(self, task: str, result: str, *,
                          child_session_id: str = "", **kwargs) -> None:
            pass

        def get_config_schema(self) -> List[Dict[str, Any]]:
            return []

        def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
            pass

        def on_memory_write(
            self,
            action: str,
            target: str,
            content: str,
            metadata: Optional[Dict[str, Any]] = None,
        ) -> None:
            pass

        def backup_paths(self) -> List[str]:
            return []


try:  # pragma: no cover - exercised only inside a hermes runtime
    from tools.registry import tool_error  # type: ignore
except ImportError:
    def tool_error(message, **extra) -> str:  # type: ignore[no-redef]
        """Return a JSON error string for tool handlers (hermes-compatible)."""
        result: Dict[str, Any] = {"error": str(message)}
        result.update(extra)
        return json.dumps(result)


__all__ = ["MemoryProvider", "tool_error"]
