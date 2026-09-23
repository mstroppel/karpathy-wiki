"""HTTP access to the Paperless-ngx API."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any, Protocol


class PaperlessSource(Protocol):
    """The subset of Paperless API behavior the ingestor depends on."""

    def selected_document_ids(self) -> list[int]: ...

    def document(self, document_id: int) -> dict[str, Any]: ...

    def document_type_name(self, value: Any) -> str | None: ...

    def tag_names(self, values: Any) -> list[str]: ...


def resource_id(value: Any, resource: str) -> int:
    if isinstance(value, dict):
        value = value.get("id")
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Paperless {resource} entry has no numeric ID") from error


class PaperlessClient:
    def __init__(self, public_url: str, token: str, source_tag_id: int) -> None:
        self.public_url = public_url.rstrip("/")
        self.source_tag_id = source_tag_id
        self.headers = {
            "Accept": "application/json; version=10",
            "Authorization": f"Token {token}",
            "User-Agent": "karpathy-wiki-ingest/1",
        }
        self.resource_names: dict[tuple[str, int], str] = {}

    def selected_document_ids(self) -> list[int]:
        self.resource_names.clear()
        document_ids: set[int] = set()
        page = 1
        while True:
            query = urllib.parse.urlencode(
                {
                    "tags__id__all": self.source_tag_id,
                    "ordering": "id",
                    "page": page,
                    "page_size": 100,
                }
            )
            payload = self._get_json(f"{self.public_url}/api/documents/?{query}")
            results = payload.get("results")
            if not isinstance(results, list):
                raise ValueError("Paperless document list has no results array")
            try:
                document_ids.update(int(item["id"]) for item in results)
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError("Paperless document list has malformed entries") from error
            if not payload.get("next"):
                break
            page += 1
        return sorted(document_ids)

    def document(self, document_id: int) -> dict[str, Any]:
        payload = self._get_json(f"{self.public_url}/api/documents/{document_id}/")
        if int(payload.get("id", -1)) != document_id:
            raise ValueError(f"Paperless returned the wrong document for {document_id}")
        return payload

    def document_type_name(self, value: Any) -> str | None:
        if value is None:
            return None
        return self._resource_name("document_types", value)

    def tag_names(self, values: Any) -> list[str]:
        if not isinstance(values, list):
            raise ValueError("Paperless document tags are not an array")
        return [self._resource_name("tags", value) for value in values]

    def _resource_name(self, resource: str, value: Any) -> str:
        if isinstance(value, dict):
            name = value.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"Paperless {resource} entry has no name")
            return " ".join(name.split())
        try:
            resource_id_value = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"Paperless {resource} entry has no numeric ID") from error
        key = (resource, resource_id_value)
        if key not in self.resource_names:
            payload = self._get_json(f"{self.public_url}/api/{resource}/{resource_id_value}/")
            name = payload.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"Paperless {resource} entry has no name")
            self.resource_names[key] = " ".join(name.split())
        return self.resource_names[key]

    def _get_json(self, url: str) -> dict[str, Any]:
        return self._request_json(url)

    def _request_json(self, url: str) -> dict[str, Any]:
        request = urllib.request.Request(url, headers=self.headers)
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.load(response)
        if not isinstance(payload, dict):
            raise ValueError("Paperless returned a non-object JSON response")
        return payload
