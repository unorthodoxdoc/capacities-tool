"""Capacities REST API client with retry, rate limiting, and error handling.

Implements the documented Capacities API surface:
https://developers.capacities.io/
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional
from urllib.parse import urljoin

import httpx

from .config import CapacitiesConfig


class CapacitiesError(Exception):
    """Base exception for Capacities API failures."""

    def __init__(self, message: str, status_code: Optional[int] = None, body: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class RateLimitExceeded(CapacitiesError):
    """Raised when the API reports a rate limit (429)."""


class _TokenBucket:
    """Simple token-bucket rate limiter shared across client instances."""

    def __init__(self, rate: float, per: float = 60.0):
        self.rate = rate  # tokens per `per` seconds
        self.per = per
        self._tokens = 0.0
        self._last = time.monotonic()
        self._lock = __import__("threading").Lock()

    def acquire(self) -> None:
        """Block until a token is available, then consume it."""
        import threading

        with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._last
                self._last = now
                self._tokens += elapsed * (self.rate / self.per)
                if self._tokens > self.rate:
                    self._tokens = self.rate
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                # Sleep a fraction of the refill interval and retry.
                time.sleep(max(0.01, self.per / self.rate * 0.1))


# Per-endpoint rate limits documented by Capacities (requests per 60s).
# Structures: 10, Objects: 30, Blocks: 30. We use a conservative 15 req/s
# global bucket plus per-endpoint buckets to stay well under limits.
_RATE_LIMITS: Dict[str, float] = {
    "structures": 10.0,
    "object": 30.0,
    "objects": 30.0,
    "blocks": 30.0,
    "search": 30.0,
    "default": 15.0,
}


def _rate_key(path: str) -> str:
    for key in _RATE_LIMITS:
        if key != "default" and key in path:
            return key
    return "default"


class CapacitiesClient:
    """Thin REST client for the Capacities API.

    Handles authentication, versioning, rate limiting, retries, and
    error translation. All paths are relative to ``base_url``.
    """

    def __init__(self, config: CapacitiesConfig):
        self.config = config
        self._client = httpx.Client(
            base_url=config.base_url,
            headers={
                "Authorization": config.auth_header,
                "X-Capacities-Api-Version": config.api_version,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(30.0),
        )
        # One bucket per endpoint category + a global bucket.
        # Lazily initialized so that tests which construct the client
        # via __new__ (bypassing __init__) still get working buckets.
        self._buckets_instance: Dict[str, _TokenBucket] = {}
        self._global_bucket_instance: _TokenBucket = _TokenBucket(rate=12.0, per=60.0)

    @property
    def _buckets(self) -> Dict[str, _TokenBucket]:
        if not hasattr(self, '_buckets_instance'):
            self._buckets_instance: Dict[str, _TokenBucket] = {}
        return self._buckets_instance

    @property
    def _global_bucket(self) -> _TokenBucket:
        if not hasattr(self, '_global_bucket_instance'):
            self._global_bucket_instance = _TokenBucket(rate=12.0, per=60.0)
        return self._global_bucket_instance

    def _bucket(self, path: str) -> _TokenBucket:
        key = _rate_key(path)
        if key not in self._buckets:
            self._buckets[key] = _TokenBucket(rate=_RATE_LIMITS[key], per=60.0)
        return self._buckets[key]

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
        retries: int = 3,
    ) -> Dict[str, Any]:
        """Execute an HTTP request with rate limiting, retries, and error handling."""
        url = urljoin(self.config.base_url, path.lstrip("/"))

        # Acquire rate-limit tokens (global + endpoint-specific).
        self._global_bucket.acquire()
        self._bucket(path).acquire()

        last_error: Optional[Exception] = None
        for attempt in range(retries):
            try:
                if files:
                    response = self._client.request(
                        method, url, params=params, data=data, files=files
                    )
                else:
                    response = self._client.request(
                        method, url, params=params, json=data
                    )
            except httpx.HTTPError as exc:
                last_error = CapacitiesError(
                    f"HTTP error on {method} {url}: {exc}",
                    status_code=getattr(exc.response, "status_code", None),
                )
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise last_error

            # Handle rate limiting.
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After") or response.headers.get(
                    "RateLimit"
                )
                wait = float(retry_after) if retry_after else 5.0
                last_error = RateLimitExceeded(
                    f"Rate limited on {method} {url}. Retry after {wait}s.",
                    status_code=429,
                    body=response.text,
                )
                if attempt < retries - 1:
                    time.sleep(wait)
                    continue
                raise last_error

            # Handle client/server errors.
            if 400 <= response.status_code < 600:
                last_error = CapacitiesError(
                    f"API error {response.status_code} on {method} {url}: "
                    f"{response.text[:500]}",
                    status_code=response.status_code,
                    body=response.text,
                )
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise last_error

            # Success: parse JSON body.
            try:
                return response.json()
            except ValueError:
                return {"_raw": response.text}

        # Should not reach here, but be safe.
        raise last_error or CapacitiesError("Request failed after retries.")

    # ------------------------------------------------------------------
    # Space / structures
    # ------------------------------------------------------------------
    def space(self) -> Dict[str, Any]:
        """Get the authenticated space (token is bound to one space)."""
        return self._request("GET", "/space")

    def structures(self) -> Dict[str, Any]:
        """List all object types (structures) in the space."""
        return self._request("GET", "/space/structures")

    def structure(self, structure_id: str) -> Dict[str, Any]:
        """Get a single structure by id."""
        return self._request("GET", f"/space/structures/{structure_id}")

    # ------------------------------------------------------------------
    # Objects (CRUD)
    # ------------------------------------------------------------------
    def get_object(self, object_id: str) -> Dict[str, Any]:
        """Read a single object by id."""
        return self._request("GET", f"/object?id={object_id}")

    def create_object(
        self,
        structure_id: str,
        properties: Optional[Dict[str, Any]] = None,
        collections: Optional[list[str]] = None,
        blocks: Optional[Dict[str, list[Dict[str, Any]]]] = None,
    ) -> Dict[str, Any]:
        """Create a new object of the given structure."""
        body: Dict[str, Any] = {"structureId": structure_id}
        if properties is not None:
            body["properties"] = properties
        if collections is not None:
            body["collections"] = collections
        if blocks is not None:
            body["blocks"] = blocks
        return self._request("POST", "/object", data=body)

    def update_object(
        self,
        object_id: str,
        properties: Optional[Dict[str, Any]] = None,
        collections: Optional[list[str]] = None,
        blocks: Optional[Dict[str, list[Dict[str, Any]]]] = None,
    ) -> Dict[str, Any]:
        """Update an existing object (PATCH). At least one of
        ``properties`` or ``collections`` must be provided."""
        if properties is None and collections is None and blocks is None:
            raise ValueError("At least one of properties, collections, or blocks is required.")
        body: Dict[str, Any] = {"id": object_id}
        if properties is not None:
            body["properties"] = properties
        if collections is not None:
            body["collections"] = collections
        if blocks is not None:
            body["blocks"] = blocks
        return self._request("PATCH", "/object", data=body)

    def delete_object(self, object_id: str, hard_delete: bool = False) -> Dict[str, Any]:
        """Delete an object. ``hard_delete=True`` permanently removes it."""
        params = {}
        if hard_delete:
            params["hardDelete"] = "true"
        return self._request("DELETE", f"/object?id={object_id}", params=params)

    def list_objects_by_structure(
        self, structure_id: str, page_size: int = 100, cursor: Optional[str] = None
    ) -> Dict[str, Any]:
        """List objects of a given structure (cursor-paginated)."""
        params: Dict[str, Any] = {"id": structure_id, "pageSize": page_size}
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", f"/objects/structure", params=params)

    def list_objects_by_tag(self, tag_id: str, page_size: int = 100) -> Dict[str, Any]:
        """List objects tagged with the given tag id."""
        return self._request(
            "GET", f"/objects/tag", params={"id": tag_id, "pageSize": page_size}
        )

    def list_objects_by_collection(
        self, collection_id: str, page_size: int = 100, cursor: Optional[str] = None
    ) -> Dict[str, Any]:
        """List objects in a given collection (database)."""
        params: Dict[str, Any] = {"id": collection_id, "pageSize": page_size}
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", f"/objects/collection", params=params)

    def search_objects(self, query: str) -> Dict[str, Any]:
        """Search objects by title/relevance (single page)."""
        return self._request("POST", "/objects/search", data={"query": query})

    # ------------------------------------------------------------------
    # Blocks
    # ------------------------------------------------------------------
    def append_blocks(
        self,
        object_id: str,
        property_id: str,
        blocks: list[Dict[str, Any]],
        position: Optional[Dict[str, Any]] = None,
        parent_block_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Append blocks to an object's block property."""
        body: Dict[str, Any] = {
            "id": object_id,
            "propertyId": property_id,
            "blocks": blocks,
        }
        if position is not None:
            body["position"] = position
        if parent_block_id is not None:
            body["parentBlockId"] = parent_block_id
        return self._request("POST", "/blocks/append", data=body)

    def append_markdown(
        self,
        object_id: str,
        property_id: str,
        markdown: str,
        position: Optional[Dict[str, Any]] = None,
        parent_block_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Append content as Markdown to an object's block property."""
        body: Dict[str, Any] = {
            "id": object_id,
            "propertyId": property_id,
            "markdown": markdown,
        }
        if position is not None:
            body["position"] = position
        if parent_block_id is not None:
            body["parentBlockId"] = parent_block_id
        return self._request("POST", "/blocks/markdown/append", data=body)

    def patch_block(
        self,
        block_id: str,
        block: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Replace the content of a single block (id preserved)."""
        return self._request("PATCH", f"/blocks/block", data={"id": block_id, **block})

    def delete_blocks(self, block_ids: list[str]) -> Dict[str, Any]:
        """Delete one or more blocks (subtrees removed)."""
        return self._request("POST", "/blocks/delete", data={"ids": block_ids})

    # ------------------------------------------------------------------
    # Markdown import/export
    # ------------------------------------------------------------------
    def export_markdown(self, object_id: str) -> str:
        """Export an object's content as Markdown."""
        result = self._request("GET", f"/object/markdown?id={object_id}")
        return result.get("markdown", "")

    def create_from_markdown(
        self,
        structure_id: str,
        markdown: str,
        collections: Optional[list[str]] = None,
    ) -> Dict[str, Any]:
        """Create an object from Markdown source."""
        body: Dict[str, Any] = {"structureId": structure_id, "markdown": markdown}
        if collections is not None:
            body["collections"] = collections
        return self._request("POST", "/object/markdown", data=body)

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()
