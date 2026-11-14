from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import api_router, router
from app.config import get_settings
from app.observability import (
    MaxBodySizeMiddleware,
    RequestIdMiddleware,
    configure_logging,
)
from app.review.routes import router as review_router
from app.review.security import LocalReviewSecurityMiddleware, allowed_origins
from app.review.store import ReviewStore
from app.web.routes import router as web_router


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        allowed_origins()
        store = ReviewStore(get_settings().REVIEW_DATABASE_PATH)
        store.start()
        application.state.review_store = store
        try:
            yield
        finally:
            store.close()

    app = FastAPI(
        lifespan=lifespan,
        title=settings.APP_NAME,
        docs_url=f"{settings.API_PREFIX}/docs",
        redoc_url=f"{settings.API_PREFIX}/redoc",
        openapi_url=f"{settings.API_PREFIX}/openapi.json",
        # FastAPI otherwise registers the Swagger OAuth2 redirect helper at the
        # default, unprefixed /docs/oauth2-redirect, regardless of docs_url. This PoC
        # has no OAuth flow in Swagger, so disable it: no unprefixed docs endpoint is
        # exposed and the whole docs surface stays under API_PREFIX.
        swagger_ui_oauth2_redirect_url=None,
    )
    # Order matters. add_middleware adds outermost-last, so RequestIdMiddleware ends
    # up outermost: it assigns/echoes X-Request-ID for every response, including the
    # 413 that MaxBodySizeMiddleware (just inside it) returns for oversized bodies.
    app.add_middleware(MaxBodySizeMiddleware, max_bytes=settings.MAX_REQUEST_BODY_BYTES)
    app.add_middleware(LocalReviewSecurityMiddleware)
    app.add_middleware(RequestIdMiddleware)
    app.include_router(router)
    app.include_router(api_router, prefix=settings.API_PREFIX)
    app.include_router(review_router, prefix=settings.API_PREFIX)
    app.include_router(web_router)
    return app


app = create_app()
