"""Serve the local review page without a template or frontend dependency."""

from html import escape
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse, Response

from app.config import get_settings

router = APIRouter()
_STATIC = Path(__file__).parent / "static"
_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
}


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def review_page() -> HTMLResponse:
    markup = (_STATIC / "index.html").read_text(encoding="utf-8")
    markup = markup.replace("__API_PREFIX__", escape(get_settings().API_PREFIX, quote=True))
    return HTMLResponse(markup, headers=_HEADERS)


@router.get("/assets/review.js", include_in_schema=False)
def review_script() -> FileResponse:
    return FileResponse(_STATIC / "review.js", media_type="text/javascript", headers=_HEADERS)


@router.get("/assets/review.css", include_in_schema=False)
def review_styles() -> FileResponse:
    return FileResponse(_STATIC / "review.css", media_type="text/css", headers=_HEADERS)


@router.get("/favicon.ico", include_in_schema=False)
def empty_favicon() -> Response:
    return Response(status_code=204, headers=_HEADERS)
