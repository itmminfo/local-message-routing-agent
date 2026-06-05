from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings


class OllamaUnavailableError(RuntimeError):
    """Raised when the Ollama API cannot be reached."""


class OllamaChatError(RuntimeError):
    """Raised when Ollama returns an invalid chat response."""


@dataclass(frozen=True)
class OllamaReadiness:
    """Outcome of the Ollama readiness probe: whether the API is reachable and
    whether the configured model is actually present in /api/tags."""

    api_reachable: bool
    model_available: bool


class OllamaClient:
    def __init__(
        self,
        host: str | None = None,
        model: str | None = None,
        timeout_seconds: int | None = None,
        num_predict: int | None = None,
    ) -> None:
        settings = get_settings()
        self.host = (host or settings.OLLAMA_HOST).rstrip("/")
        self.model = model or settings.OLLAMA_MODEL
        self.timeout_seconds = timeout_seconds or settings.REQUEST_TIMEOUT_SECONDS
        self.num_predict = num_predict or settings.OLLAMA_NUM_PREDICT

    def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "stream": False,
            "options": {"temperature": 0, "num_predict": self.num_predict},
        }

        # Network-level retries/backoff are intentionally omitted in this PoC: a
        # transient transport error surfaces as HTTP 503 and the caller can retry.
        # The agent-level retry handles malformed tool calls, not connectivity.
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.post(f"{self.host}/api/chat", json=payload)
        except httpx.TransportError as exc:
            raise OllamaUnavailableError(f"Ollama is unavailable at {self.host}: {exc}") from exc

        if not response.is_success:
            raise OllamaChatError(
                f"Ollama chat request failed with HTTP {response.status_code}: {response.text}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise OllamaChatError("Ollama returned invalid JSON.") from exc

        if not isinstance(data, dict):
            raise OllamaChatError("Ollama returned an unexpected response shape.")

        return data

    def check_readiness(self, timeout_seconds: float = 5.0) -> OllamaReadiness:
        # Readiness probe (not liveness): hit /api/tags with a short timeout (not the
        # long inference timeout) so /ready answers quickly, and report two distinct
        # signals: is the API reachable, and is the configured model actually pulled?
        # A reachable API whose model was removed (or whose OLLAMA_MODEL was changed to
        # something not pulled) is not ready to route, so the two are reported apart.
        try:
            with httpx.Client(timeout=timeout_seconds) as client:
                response = client.get(f"{self.host}/api/tags")
        except httpx.TransportError:
            return OllamaReadiness(api_reachable=False, model_available=False)

        if not response.is_success:
            return OllamaReadiness(api_reachable=False, model_available=False)

        try:
            data = response.json()
        except ValueError:
            # The API answered but the body is not JSON: it is reachable, but the
            # model's presence cannot be confirmed, so report the model as unavailable.
            return OllamaReadiness(api_reachable=True, model_available=False)

        return OllamaReadiness(
            api_reachable=True,
            model_available=self._model_in_tags(data),
        )

    def _model_in_tags(self, data: Any) -> bool:
        # Ollama /api/tags returns {"models": [{"name": "qwen2.5:3b", "model": ...}, ...]}.
        # Be defensive about the shape (any field may be missing or the wrong type) and
        # match the configured model against either the "name" or "model" field.
        #
        # Ollama stores an untagged model (e.g. "qwen2.5") as "<name>:latest", so accept
        # that form too, mirroring scripts/wait-for-ollama-and-pull-model.sh. Without
        # it an untagged OLLAMA_MODEL would read as "missing" here while the init script
        # considers it present.
        if not isinstance(data, dict):
            return False
        models = data.get("models")
        if not isinstance(models, list):
            return False
        candidates = {self.model}
        if ":" not in self.model:
            candidates.add(f"{self.model}:latest")
        for entry in models:
            if not isinstance(entry, dict):
                continue
            for key in ("name", "model"):
                value = entry.get(key)
                if isinstance(value, str) and value in candidates:
                    return True
        return False
