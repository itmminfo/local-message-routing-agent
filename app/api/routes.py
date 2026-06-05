import asyncio
import logging
import smtplib
import threading

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from app.agent.routing_agent import RoutingAgent
from app.application.message_routing_service import MessageRoutingService
from app.config import get_settings
from app.infrastructure.mail_sender import MailSender
from app.infrastructure.ollama_client import (
    OllamaChatError,
    OllamaClient,
    OllamaUnavailableError,
)
from app.schemas.messages import ErrorResponse, MessageRouteRequest, MessageRouteResponse

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
        mail_sender=mail_sender,
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
    response_model=MessageRouteResponse,
    summary="Route a message to an internal department",
    description=(
        "Accepts a sender email and message, asks Ollama to call the "
        "`send_email` tool, validates the selected recipient, and sends the "
        "message through SMTP. `Reply-To` is always set by the application "
        "from the original request email."
    ),
    responses={
        400: {
            "description": "The request could not be processed.",
            "model": ErrorResponse,
        },
        413: {
            "description": "Request body too large.",
            "model": ErrorResponse,
        },
        422: {"description": "Validation error."},
        429: {
            "description": "Too many concurrent routing requests.",
            "model": ErrorResponse,
        },
        502: {
            "description": (
                "Upstream failure: the Ollama chat request failed, or the email "
                "could not be delivered via SMTP."
            ),
            "model": ErrorResponse,
        },
        503: {
            "description": "Ollama is unavailable.",
            "model": ErrorResponse,
        },
    },
    tags=["messages"],
)
def route_message(
    request: MessageRouteRequest,
    service: MessageRoutingService = Depends(get_message_routing_service),
) -> MessageRouteResponse:
    if not _route_semaphore.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail="Too many concurrent routing requests; retry shortly.",
        )
    try:
        return service.route_message(request)
    except OllamaUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Ollama is unavailable.") from exc
    except OllamaChatError as exc:
        raise HTTPException(
            status_code=502,
            detail="Ollama chat request failed.",
        ) from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise HTTPException(status_code=502, detail="Email delivery failed.") from exc
    except ValueError as exc:
        # Defensive boundary: the recipient is already validated against the enum and
        # headers are CRLF-sanitized upstream, so a ValueError here is unexpected.
        # Return a generic 400 and log the cause server-side instead of leaking it.
        logger.warning("Rejected message route request: %s", exc)
        raise HTTPException(
            status_code=400,
            detail="The request could not be processed.",
        ) from exc
    finally:
        _route_semaphore.release()
