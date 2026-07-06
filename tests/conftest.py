from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import httpx
import pytest

from hermes_gnosis import GnosisMemoryProvider
from hermes_gnosis._client import GnosisClient


class RecordingApp:
    """MockTransport handler that records requests and serves canned replies.

    ``responses`` maps ``(method, path)`` -> (status_code, json_body).
    Unmatched requests get a 200 with an empty results payload.
    """

    def __init__(self, responses: Optional[Dict[tuple, tuple]] = None,
                 default_status: int = 200,
                 default_body: Optional[dict] = None):
        self.requests: List[httpx.Request] = []
        self.responses = responses or {}
        self.default_status = default_status
        self.default_body = default_body if default_body is not None else {"results": []}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        if key in self.responses:
            status, body = self.responses[key]
            return httpx.Response(status, json=body)
        return httpx.Response(self.default_status, json=self.default_body)

    def body(self, index: int = -1) -> Dict[str, Any]:
        return json.loads(self.requests[index].content.decode("utf-8"))


def make_client(app: RecordingApp, **kwargs) -> GnosisClient:
    return GnosisClient(
        "https://gnosis.test",
        "test-token",
        transport=httpx.MockTransport(app),
        **kwargs,
    )


def make_provider(app: RecordingApp, *, session_id: str = "sess-1",
                  **init_kwargs) -> GnosisMemoryProvider:
    """Build an initialized provider wired to a MockTransport client."""
    provider = GnosisMemoryProvider()
    provider._config = {
        "gnosis_url": "https://gnosis.test",
        "gnosis_token": "test-token",
        "agent_id": "hermes",
        "tenant_id": "nolgia",
        "timeout": 5.0,
        "add_timeout": 5.0,
    }
    provider._session_id = session_id
    provider._user_id = init_kwargs.pop("user_id", "hermes-user")
    provider._channel = init_kwargs.pop("platform", "cli")
    provider._agent_context = init_kwargs.pop("agent_context", "primary")
    provider._client = make_client(app)
    return provider


@pytest.fixture
def app() -> RecordingApp:
    return RecordingApp()
