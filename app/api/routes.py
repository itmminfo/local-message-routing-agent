import asyncio
import logging
import threading

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from app.agent.routing_agent import RoutingAgent
from app.application.message_routing_service import MessageRoutingService
from app.config import get_settings
from app.infrastructure.mail_sender import MailSender
from app.infrastructure.ollama_client import (
    OllamaClient,
)
from app.schemas.messages import ErrorResponse

logger = logging.getLogger(__name__)

router = APIRouter()
api_router = APIRouter()

# Bounds concurrent routing so sync handlers cannot exhaust the anyio threadpool;
# excess requests get 429. Per-process, so under N workers the cap is N * value.
_route_semaphore = threading.BoundedSemaphore(get_settings().MAX_CONCURRENT_ROUTE_REQUESTS)

# Short timeout for the /ready probes, independent of the longer request/SMTP send
# timeouts, so readiness answers quickly.
_READY_PROBE_TIMEOUT_SECONDS = 3.0


@router.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    # Liveness only, and async on purpose: it runs on the event loop, never the
    # threadpool, so a saturated threadpool (slow inference) cannot starve it.
    return {"status": "ok"}


def get_ollama_client() -> OllamaClient:
    settings = get_settings()
    return OllamaClient(
        host=settings.OLLAMA_HOST,
        model=settings.OLLAMA_MODEL,
        timeout_seconds=settings.REQUEST_TIMEOUT_SECONDS,
        num_predict=settings.OLLAMA_NUM_PREDICT,
        think=settings.OLLAMA_THINK,
    )


def get_mail_sender() -> MailSender:
    settings = get_settings()
    return MailSender(
        smtp_host=settings.SMTP_HOST,
        smtp_port=settings.SMTP_PORT,
        mail_from=settings.MAIL_FROM,
    )


def get_routing_agent(
    ollama_client: OllamaClient = Depends(get_ollama_client),
    mail_sender: MailSender = Depends(get_mail_sender),
) -> RoutingAgent:
    return RoutingAgent(
        ollama_client=ollama_client,
    )


def get_message_routing_service(
    routing_agent: RoutingAgent = Depends(get_routing_agent),
) -> MessageRoutingService:
    return MessageRoutingService(routing_agent=routing_agent)


@router.get("/ready", tags=["system"])
async def ready(
    ollama_client: OllamaClient = Depends(get_ollama_client),
    mail_sender: MailSender = Depends(get_mail_sender),
) -> JSONResponse:
    # Readiness (vs the /health liveness probe): report whether Ollama is reachable,
    # whether the configured model is actually present, and whether the SMTP sink is
    # reachable. The blocking probes run concurrently (each in a thread) with a short
    # timeout, so /ready stays cheap instead of summing the timeouts.
    ollama_readiness, smtp_ok = await asyncio.gather(
        asyncio.to_thread(ollama_client.check_readiness, _READY_PROBE_TIMEOUT_SECONDS),
        asyncio.to_thread(mail_sender.is_ready, _READY_PROBE_TIMEOUT_SECONDS),
    )
    checks = {
        "ollama_api": ollama_readiness.api_reachable,
        "ollama_model": ollama_readiness.model_available,
        "smtp": smtp_ok,
    }
    ready_ok = all(checks.values())
    return JSONResponse(
        status_code=200 if ready_ok else 503,
        content={"status": "ready" if ready_ok else "not ready", "checks": checks},
    )


@api_router.post(
    "/messages/route",
    tags=["messages"],
    deprecated=True,
    status_code=409,
    responses={
        409: {"description": "Human review is required before delivery.", "model": ErrorResponse}
    },
)
@api_router.post("/messages/route-local-eval", include_in_schema=False, status_code=409)
def route_message() -> None:
    raise HTTPException(status_code=409, detail="Human review is required before delivery.")
