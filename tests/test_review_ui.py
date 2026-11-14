import asyncio
from html.parser import HTMLParser

import httpx
import pytest
from app.web.routes import router
from fastapi import FastAPI


def get_page(path: str) -> httpx.Response:
    target = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    target.include_router(router)

    async def call_app() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=target), base_url="http://testserver"
        ) as client:
            return await client.get(path)

    return asyncio.run(call_app())


class PageElements(HTMLParser):
    def __init__(self, markup: str) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.feed(markup)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs)))

    def by_id(self, element_id: str) -> dict[str, str | None]:
        return next(attrs for _, attrs in self.elements if attrs.get("id") == element_id)


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/", "text/html"),
        ("/assets/review.js", "text/javascript"),
        ("/assets/review.css", "text/css"),
    ],
)
def test_review_page_and_assets_have_private_same_origin_policy(
    path: str, content_type: str
) -> None:
    response = get_page(path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(content_type)
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "DENY"
    policy = response.headers["content-security-policy"]
    for directive in (
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "connect-src 'self'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ):
        assert directive in policy
    assert "unsafe-inline" not in policy
    assert "access-control-allow-origin" not in response.headers


def test_initial_page_requires_explicit_department_and_confirmation() -> None:
    page = PageElements(get_page("/").text)

    assert page.by_id("sender-email")["type"] == "email"
    assert "required" in page.by_id("sender-email")
    assert "required" in page.by_id("message")
    assert "disabled" in page.by_id("create-preview")
    assert "required" in page.by_id("department")
    options = [attrs for tag, attrs in page.elements if tag == "option"]
    assert options == [{"value": ""}]
    confirmation = page.by_id("confirmed")
    assert confirmation["type"] == "checkbox"
    assert "required" in confirmation
    assert "checked" not in confirmation
    assert "disabled" in page.by_id("approve-send")
    assert "hidden" in page.by_id("retry-send")
    assert page.by_id("state-label")["aria-live"] == "polite"
    assert page.by_id("original-message")["tabindex"] == "0"


def test_recovery_controls_preserve_an_unread_result_and_require_a_separate_warning() -> None:
    page = PageElements(get_page("/").text)

    assert "hidden" in page.by_id("recovery-panel")
    assert page.by_id("resolve-status")["type"] == "button"
    assert page.by_id("withdraw-preview")["type"] == "button"
    assert "hidden" in page.by_id("withdraw-preview")
    assert page.by_id("accept-context-loss")["type"] == "checkbox"
    assert "checked" not in page.by_id("accept-context-loss")
    assert "disabled" in page.by_id("abandon-context")
    assert "hidden" in page.by_id("abandon-context")
    assert "hidden" in page.by_id("previous-operation")


def test_page_loads_only_explicit_local_assets_and_has_no_inline_handlers() -> None:
    page = PageElements(get_page("/").text)

    sources = [attrs["src"] for _, attrs in page.elements if "src" in attrs]
    assert sources == ["/assets/review.js"]
    styles = [attrs["href"] for tag, attrs in page.elements if tag == "link"]
    assert styles == ["/assets/review.css"]
    assert all(not attr.startswith("on") for _, attrs in page.elements for attr in attrs)
    assert all("src" in attrs for tag, attrs in page.elements if tag == "script")
    assert all("action" not in attrs for tag, attrs in page.elements if tag == "form")


def test_api_prefix_is_injected_as_escaped_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    prefix = '/custom/"quoted"&<review>'
    monkeypatch.setenv("API_PREFIX", prefix)

    response = get_page("/")
    page = PageElements(response.text)
    metadata = next(
        attrs
        for tag, attrs in page.elements
        if tag == "meta" and attrs.get("name") == "review-api-prefix"
    )
    assert metadata["content"] == prefix
    assert 'content="/custom/&quot;quoted&quot;&amp;&lt;review&gt;"' in response.text
    assert "__API_PREFIX__" not in response.text


@pytest.mark.parametrize("path", ["/assets/index.html", "/assets/routes.py", "/assets/not-found"])
def test_asset_routes_do_not_expose_arbitrary_files(path: str) -> None:
    assert get_page(path).status_code == 404


def test_empty_local_favicon_does_not_require_an_external_resource() -> None:
    response = get_page("/favicon.ico")
    assert response.status_code == 204
    assert response.content == b""
