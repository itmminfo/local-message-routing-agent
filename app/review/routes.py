import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.agent.routing_agent import RoutingAgent
from app.config import get_settings
from app.domain.departments import DepartmentEmail
from app.domain.routing_policy import DEPARTMENT_ROUTING_RULES
from app.infrastructure.mail_sender import LocalMailpitRequired, MailSender
from app.infrastructure.ollama_client import OllamaClient
from app.review.models import (
    ApproveRequest,
    CreatePreviewRequest,
    DepartmentOption,
    PreviewRecord,
    RetryRequest,
    SessionResponse,
    VersionRequest,
)
from app.review.security import (
    SESSION_COOKIE,
    ReviewSession,
    get_review_store,
    require_operator,
    require_session,
    session_key,
)
from app.review.service import ReviewService
from app.review.store import ReviewConflict, ReviewNotFound, ReviewStore

router = APIRouter(prefix="/review", tags=["review"])


def get_review_proposer() -> RoutingAgent:
    return RoutingAgent(OllamaClient())


def get_review_sender() -> MailSender:
    return MailSender()


def get_review_service(
    store: ReviewStore = Depends(get_review_store),
    proposer: RoutingAgent = Depends(get_review_proposer),
    sender: MailSender = Depends(get_review_sender),
) -> ReviewService:
    return ReviewService(store, proposer, sender)


Store = Annotated[ReviewStore, Depends(get_review_store)]
Operator = Annotated[ReviewSession, Depends(require_operator)]
Session = Annotated[ReviewSession, Depends(require_session)]
Service = Annotated[ReviewService, Depends(get_review_service)]


def translate_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ReviewNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=409, detail=str(exc))


@router.get("/session", response_model=SessionResponse)
def review_session(request: Request, response: Response, store: Store) -> SessionResponse:
    token = request.cookies.get(SESSION_COOKIE, "")
    record = store.get_session(session_key(token)) if token and len(token) <= 200 else None
    if record is None:
        token, csrf_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        settings = get_settings()
        store.create_session(session_key(token), csrf_token, settings.REVIEW_SESSION_TTL_SECONDS)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=settings.REVIEW_SESSION_TTL_SECONDS,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            path="/",
        )
    else:
        csrf_token = str(record["csrf_token"])
    return SessionResponse(
        csrf_token=csrf_token,
        departments=[
            DepartmentOption(recipient=d, label=DEPARTMENT_ROUTING_RULES[d]["label"])
            for d in DepartmentEmail
        ],
    )


@router.post("/previews", response_model=PreviewRecord)
def create_preview(
    payload: CreatePreviewRequest, session: Operator, service: Service
) -> PreviewRecord:
    return service.create_preview(session.key, str(payload.email), payload.message)


@router.get("/previews/{preview_id}", response_model=PreviewRecord)
def get_preview(preview_id: str, session: Session, store: Store) -> PreviewRecord:
    try:
        return store.get_preview(preview_id, session.key)
    except ReviewNotFound as exc:
        raise translate_error(exc) from exc


@router.post("/previews/{preview_id}/suggest", response_model=PreviewRecord)
def suggest(
    preview_id: str, payload: VersionRequest, session: Operator, service: Service
) -> PreviewRecord:
    try:
        return service.suggest(preview_id, session.key, payload.version)
    except (ReviewConflict, ReviewNotFound) as exc:
        raise translate_error(exc) from exc


@router.post("/previews/{preview_id}/approve", response_model=PreviewRecord)
def approve(
    preview_id: str, payload: ApproveRequest, session: Operator, service: Service
) -> PreviewRecord:
    try:
        return service.approve(
            preview_id,
            session.key,
            payload.version,
            payload.payload_digest,
            payload.recipient.value,
        )
    except (ReviewConflict, ReviewNotFound, LocalMailpitRequired) as exc:
        raise translate_error(exc) from exc


@router.post("/previews/{preview_id}/retry", response_model=PreviewRecord)
def retry(
    preview_id: str, payload: RetryRequest, session: Operator, service: Service
) -> PreviewRecord:
    try:
        return service.retry(preview_id, session.key, payload.version, payload.payload_digest)
    except (ReviewConflict, ReviewNotFound, LocalMailpitRequired) as exc:
        raise translate_error(exc) from exc


@router.post("/previews/{preview_id}/reject", response_model=PreviewRecord)
def reject(
    preview_id: str, payload: VersionRequest, session: Operator, store: Store
) -> PreviewRecord:
    try:
        return store.reject(preview_id, session.key, payload.version)
    except (ReviewConflict, ReviewNotFound) as exc:
        raise translate_error(exc) from exc
