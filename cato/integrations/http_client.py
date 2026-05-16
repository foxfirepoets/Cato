"""Minimal standard-library HTTP client for integrations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: str

    def as_dict(self) -> dict[str, Any]:
        parsed: Any = None
        try:
            parsed = json.loads(self.body) if self.body else None
        except json.JSONDecodeError:
            parsed = None
        return {
            "status": self.status,
            "headers": self.headers,
            "body": parsed if parsed is not None else self.body,
        }


def request_json(
    method: str,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any] | None = None,
    body_format: str = "json",
    timeout: float = 20.0,
) -> HttpResponse:
    """Issue one HTTP request using urllib only."""
    payload: bytes | None = None
    request_headers = dict(headers)

    if body is not None:
        if body_format == "form":
            payload = urlencode(body, doseq=True).encode("utf-8")
            request_headers.setdefault(
                "Content-Type", "application/x-www-form-urlencoded"
            )
        else:
            payload = json.dumps(body).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")

    req = Request(
        url=url,
        data=payload,
        headers=request_headers,
        method=method.upper(),
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            return HttpResponse(
                status=int(resp.status),
                headers=dict(resp.headers.items()),
                body=resp.read().decode("utf-8", errors="replace"),
            )
    except HTTPError as exc:
        return HttpResponse(
            status=int(exc.code),
            headers=dict(exc.headers.items()),
            body=exc.read().decode("utf-8", errors="replace"),
        )
    except URLError as exc:
        return HttpResponse(
            status=0,
            headers={},
            body=json.dumps({"error": str(exc.reason)}),
        )
