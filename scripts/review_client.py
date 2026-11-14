"""Local diagnostic client. It never chooses or approves a recipient by itself."""

import http.cookiejar
import json
import os
import urllib.parse
import urllib.request
from typing import Any, cast


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


class ReviewClient:
    def __init__(self, base_url: str, origin: str | None = None, timeout: int = 120) -> None:
        self.base_url = base_url.rstrip("/")
        target = urllib.parse.urlsplit(self.base_url)
        self.origin = origin or os.environ.get("REVIEW_ORIGIN", self.base_url)
        parsed = urllib.parse.urlsplit(self.origin)
        if (
            target.scheme != "http"
            or target.hostname not in {"localhost", "127.0.0.1", "::1", "api"}
            or target.username is not None
            or target.path
            or target.query
            or target.fragment
            or parsed.scheme != "http"
            or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("review diagnostics require a local HTTP API and loopback origin")
        self.host = parsed.netloc
        self.timeout = timeout
        # An unset value, not a credential; replaced by GET /review/session.
        self.csrf_token = ""  # nosec B105
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            NoRedirect(),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        )

    def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("request path must be local")
        headers = {"Accept": "application/json", "Host": self.host}
        data = None
        if method != "GET":
            headers.update({"Origin": self.origin, "X-CSRF-Token": self.csrf_token})
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        with self.opener.open(request, timeout=self.timeout) as response:  # nosec B310
            result: object = json.loads(response.read().decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError("review API returned a non-object response")
        return cast(dict[str, Any], result)

    def start_session(self) -> dict[str, Any]:
        result = self.request("GET", "/api/v1/review/session")
        token = result.get("csrf_token")
        if not isinstance(token, str) or not token:
            raise ValueError("review API did not return a CSRF token")
        self.csrf_token = token
        return result
