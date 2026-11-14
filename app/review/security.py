import hashlib
import hmac
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import get_settings
from app.review.store import ReviewStore

SESSION_COOKIE = "lmra_review_session"


def session_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def allowed_origins() -> frozenset[str]:
    origins = get_settings().REVIEW_ALLOWED_ORIGINS
    if not origins:
        raise ValueError("At least one explicit local review origin is required.")
    for origin in origins:
        parts = urlsplit(origin)
        if (
            parts.scheme not in {"http", "https"}
            or parts.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parts.username is not None
            or parts.password is not None
            or parts.path
            or parts.query
            or parts.fragment
            or origin != f"{parts.scheme}://{parts.netloc}"
        ):
            raise ValueError("Review origins must be exact local origins.")
        _ = parts.port  # Validate a malformed/non-numeric port, too.
    return frozenset(origins)


class LocalReviewSecurityMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers", [])
        hosts = [v.decode("latin-1") for k, v in headers if k.lower() == b"host"]
        origins = [v.decode("latin-1") for k, v in headers if k.lower() == b"origin"]
        expected = f"{scope.get('scheme', 'http')}://{hosts[0]}" if len(hosts) == 1 else ""
        try:
            permitted = expected in allowed_origins()
        except ValueError:
            permitted = False
        valid_origin = len(origins) <= 1 and (not origins or origins[0] == expected)
        if scope.get("method", "GET") not in {"GET", "HEAD"}:
            valid_origin = len(origins) == 1 and origins[0] == expected
        if not permitted or not valid_origin:
            await JSONResponse(
                {"detail": "Local Host and same-origin requests are required."},
                status_code=403,
                headers={"Cache-Control": "no-store"},
            )(scope, receive, send)
            return

        async def protected_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = list(message.get("headers", []))
                response_headers = [
                    (k, v) for k, v in response_headers if k.lower() != b"cache-control"
                ]
                response_headers.extend(
                    [
                        (b"cache-control", b"no-store"),
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"no-referrer"),
                    ]
                )
                message = {**message, "headers": response_headers}
            await send(message)

        await self.app(scope, receive, protected_send)


@dataclass(frozen=True)
class ReviewSession:
    key: str
    csrf_token: str


def get_review_store(request: Request) -> ReviewStore:
    store = getattr(request.app.state, "review_store", None)
    if not isinstance(store, ReviewStore):
        raise HTTPException(status_code=503, detail="Review storage is not ready.")
    return store


def require_session(
    request: Request,
    store: ReviewStore = Depends(get_review_store),
) -> ReviewSession:
    token = request.cookies.get(SESSION_COOKIE)
    if not token or len(token) > 200:
        raise HTTPException(status_code=403, detail="A valid local review session is required.")
    key = session_key(token)
    record = store.get_session(key)
    if record is None:
        raise HTTPException(status_code=403, detail="A valid local review session is required.")
    return ReviewSession(key, str(record["csrf_token"]))


def require_operator(
    request: Request,
    session: ReviewSession = Depends(require_session),
) -> ReviewSession:
    csrf = request.headers.getlist("x-csrf-token")
    if (
        len(csrf) != 1
        or not csrf[0].isascii()
        or not hmac.compare_digest(csrf[0], session.csrf_token)
    ):
        raise HTTPException(status_code=403, detail="A valid CSRF token is required.")
    return session
