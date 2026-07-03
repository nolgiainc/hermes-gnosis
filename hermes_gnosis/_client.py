"""HTTP client for the gnosis memory service API (v1).

Endpoint contract:

  POST   {base}/v1/memories          — add
         body {scope, messages?: [{role, content}], content?: str,
               infer: bool, metadata?: dict}
         → {results: [{memory_id, content, event}]}
  POST   {base}/v1/memories/search   — {scope, query, limit}
         → {results: [{memory_id, content, score, metadata,
                       created_at, updated_at}]}
  POST   {base}/v1/memories/list     — {scope, page, page_size}
         → {results, total, page, page_size}
  PATCH  {base}/v1/memories/{id}     — {scope, content}
  DELETE {base}/v1/memories/{id}     — {scope}

Auth: ``Authorization: Bearer <token>``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx


class GnosisError(Exception):
    """Any transport or non-2xx failure talking to the gnosis service."""

    def __init__(self, message: str, *, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class GnosisPermissionError(GnosisError):
    """403 from the gnosis service (e.g. memory editing feature-flagged off)."""


class GnosisClient:
    """Thin synchronous httpx wrapper over the gnosis memory API."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 10.0,
        add_timeout: float = 30.0,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        if not base_url:
            raise ValueError("gnosis base_url is required")
        self._add_timeout = add_timeout
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
        )

    # -- Internals ------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Dict[str, Any],
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        try:
            kwargs: Dict[str, Any] = {"json": json_body}
            if timeout is not None:
                kwargs["timeout"] = timeout
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise GnosisError(f"gnosis request failed: {exc}") from exc

        if response.status_code == 403:
            raise GnosisPermissionError(
                f"gnosis returned 403 for {method} {path}",
                status_code=403,
            )
        if response.status_code >= 400:
            detail = response.text[:500]
            raise GnosisError(
                f"gnosis API error {response.status_code} for {method} {path}: {detail}",
                status_code=response.status_code,
            )
        if not response.content:
            return {}
        try:
            data = response.json()
        except ValueError as exc:
            raise GnosisError(f"gnosis returned invalid JSON: {exc}") from exc
        return data if isinstance(data, dict) else {}

    # -- API ------------------------------------------------------------------

    def add(
        self,
        scope: Dict[str, Any],
        *,
        messages: Optional[List[Dict[str, str]]] = None,
        content: Optional[str] = None,
        infer: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Add memories: messages+infer for extraction, content for verbatim."""
        body: Dict[str, Any] = {"scope": scope, "infer": infer}
        if messages is not None:
            body["messages"] = messages
        if content is not None:
            body["content"] = content
        if metadata:
            body["metadata"] = metadata
        return self._request(
            "POST", "/v1/memories", json_body=body, timeout=self._add_timeout,
        )

    def search(
        self, scope: Dict[str, Any], query: str, *, limit: int = 10,
    ) -> List[Dict[str, Any]]:
        body = {"scope": scope, "query": query, "limit": limit}
        response = self._request("POST", "/v1/memories/search", json_body=body)
        results = response.get("results", [])
        return results if isinstance(results, list) else []

    def list(
        self, scope: Dict[str, Any], *, page: int = 1, page_size: int = 100,
    ) -> Dict[str, Any]:
        body = {"scope": scope, "page": page, "page_size": page_size}
        response = self._request("POST", "/v1/memories/list", json_body=body)
        results = response.get("results", [])
        if not isinstance(results, list):
            results = []
        return {
            "results": results,
            "total": response.get("total", len(results)),
            "page": response.get("page", page),
            "page_size": response.get("page_size", page_size),
        }

    def update(
        self, scope: Dict[str, Any], memory_id: str, content: str,
    ) -> Dict[str, Any]:
        body = {"scope": scope, "content": content}
        return self._request("PATCH", f"/v1/memories/{memory_id}", json_body=body)

    def delete(self, scope: Dict[str, Any], memory_id: str) -> Dict[str, Any]:
        body = {"scope": scope}
        return self._request("DELETE", f"/v1/memories/{memory_id}", json_body=body)

    def close(self) -> None:
        try:
            self._http.close()
        except Exception:
            pass
