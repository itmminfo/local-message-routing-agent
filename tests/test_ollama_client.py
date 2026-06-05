from unittest.mock import MagicMock, patch

import httpx
import pytest
from app.infrastructure.ollama_client import (
    OllamaChatError,
    OllamaClient,
    OllamaUnavailableError,
)


def test_chat_with_tools_posts_non_streaming_tool_chat_request() -> None:
    response = MagicMock()
    response.is_success = True
    response.json.return_value = {"message": {"tool_calls": []}}

    http_client = MagicMock()
    http_client.post.return_value = response

    messages = [{"role": "user", "content": "Nie działa mi komputer"}]
    tools = [{"type": "function", "function": {"name": "send_email"}}]

    with patch("app.infrastructure.ollama_client.httpx.Client") as client_class:
        client_class.return_value.__enter__.return_value = http_client

        result = OllamaClient(
            host="http://ollama:11434/",
            model="qwen2.5:3b",
            timeout_seconds=7,
            num_predict=64,
        ).chat_with_tools(messages, tools)

    client_class.assert_called_once_with(timeout=7)
    http_client.post.assert_called_once()

    url = http_client.post.call_args.args[0]
    payload = http_client.post.call_args.kwargs["json"]

    assert url == "http://ollama:11434/api/chat"
    assert payload == {
        "model": "qwen2.5:3b",
        "messages": messages,
        "tools": tools,
        "stream": False,
        "options": {"temperature": 0, "num_predict": 64},
    }
    assert result == {"message": {"tool_calls": []}}


def test_chat_with_tools_raises_unavailable_on_transport_error() -> None:
    http_client = MagicMock()
    http_client.post.side_effect = httpx.ConnectError("connection refused")

    with patch("app.infrastructure.ollama_client.httpx.Client") as client_class:
        client_class.return_value.__enter__.return_value = http_client

        with pytest.raises(OllamaUnavailableError, match="Ollama is unavailable"):
            OllamaClient(timeout_seconds=7).chat_with_tools([], [])


def test_chat_with_tools_raises_chat_error_on_http_error() -> None:
    response = MagicMock()
    response.is_success = False
    response.status_code = 500
    response.text = "model failed"

    http_client = MagicMock()
    http_client.post.return_value = response

    with patch("app.infrastructure.ollama_client.httpx.Client") as client_class:
        client_class.return_value.__enter__.return_value = http_client

        with pytest.raises(OllamaChatError, match="HTTP 500"):
            OllamaClient(timeout_seconds=7).chat_with_tools([], [])


def test_chat_with_tools_raises_chat_error_on_invalid_json() -> None:
    response = MagicMock()
    response.is_success = True
    response.json.side_effect = ValueError("not json")

    http_client = MagicMock()
    http_client.post.return_value = response

    with patch("app.infrastructure.ollama_client.httpx.Client") as client_class:
        client_class.return_value.__enter__.return_value = http_client

        with pytest.raises(OllamaChatError, match="invalid JSON"):
            OllamaClient(timeout_seconds=7).chat_with_tools([], [])


def test_check_readiness_true_when_api_reachable_and_model_present() -> None:
    response = MagicMock()
    response.is_success = True
    response.json.return_value = {
        "models": [
            {"name": "llama3:8b", "model": "llama3:8b"},
            {"name": "qwen2.5:3b", "model": "qwen2.5:3b", "digest": "sha256:abc"},
        ]
    }

    http_client = MagicMock()
    http_client.get.return_value = response

    with patch("app.infrastructure.ollama_client.httpx.Client") as client_class:
        client_class.return_value.__enter__.return_value = http_client

        readiness = OllamaClient(host="http://ollama:11434", model="qwen2.5:3b").check_readiness(
            3.0
        )

    assert readiness.api_reachable is True
    assert readiness.model_available is True
    assert http_client.get.call_args.args[0] == "http://ollama:11434/api/tags"
    client_class.assert_called_once_with(timeout=3.0)


def test_check_readiness_model_absent_when_configured_model_missing() -> None:
    response = MagicMock()
    response.is_success = True
    response.json.return_value = {"models": [{"name": "llama3:8b", "model": "llama3:8b"}]}

    http_client = MagicMock()
    http_client.get.return_value = response

    with patch("app.infrastructure.ollama_client.httpx.Client") as client_class:
        client_class.return_value.__enter__.return_value = http_client

        readiness = OllamaClient(host="http://ollama:11434", model="qwen2.5:3b").check_readiness()

    # API answered, but the configured model is not in the tag list.
    assert readiness.api_reachable is True
    assert readiness.model_available is False


def test_check_readiness_matches_untagged_model_as_latest() -> None:
    # Ollama stores an untagged model (e.g. "qwen2.5") as "<name>:latest". Readiness
    # must accept that form (mirroring the init script), so an untagged OLLAMA_MODEL
    # is not misreported as missing.
    response = MagicMock()
    response.is_success = True
    response.json.return_value = {"models": [{"name": "qwen2.5:latest", "model": "qwen2.5:latest"}]}

    http_client = MagicMock()
    http_client.get.return_value = response

    with patch("app.infrastructure.ollama_client.httpx.Client") as client_class:
        client_class.return_value.__enter__.return_value = http_client

        readiness = OllamaClient(host="http://ollama:11434", model="qwen2.5").check_readiness()

    assert readiness.api_reachable is True
    assert readiness.model_available is True


def test_check_readiness_false_on_transport_error() -> None:
    http_client = MagicMock()
    http_client.get.side_effect = httpx.ConnectError("refused")

    with patch("app.infrastructure.ollama_client.httpx.Client") as client_class:
        client_class.return_value.__enter__.return_value = http_client

        readiness = OllamaClient(host="http://ollama:11434", model="qwen2.5:3b").check_readiness()

    assert readiness.api_reachable is False
    assert readiness.model_available is False


def test_check_readiness_false_when_api_returns_error_status() -> None:
    response = MagicMock()
    response.is_success = False

    http_client = MagicMock()
    http_client.get.return_value = response

    with patch("app.infrastructure.ollama_client.httpx.Client") as client_class:
        client_class.return_value.__enter__.return_value = http_client

        readiness = OllamaClient(host="http://ollama:11434", model="qwen2.5:3b").check_readiness()

    assert readiness.api_reachable is False
    assert readiness.model_available is False


def test_check_readiness_model_absent_on_malformed_json() -> None:
    response = MagicMock()
    response.is_success = True
    response.json.side_effect = ValueError("not json")

    http_client = MagicMock()
    http_client.get.return_value = response

    with patch("app.infrastructure.ollama_client.httpx.Client") as client_class:
        client_class.return_value.__enter__.return_value = http_client

        readiness = OllamaClient(host="http://ollama:11434", model="qwen2.5:3b").check_readiness()

    # Reachable (HTTP 200) but body is not JSON: model presence cannot be confirmed.
    assert readiness.api_reachable is True
    assert readiness.model_available is False


def test_check_readiness_model_absent_on_unexpected_shape() -> None:
    response = MagicMock()
    response.is_success = True
    response.json.return_value = {"models": "not-a-list"}

    http_client = MagicMock()
    http_client.get.return_value = response

    with patch("app.infrastructure.ollama_client.httpx.Client") as client_class:
        client_class.return_value.__enter__.return_value = http_client

        readiness = OllamaClient(host="http://ollama:11434", model="qwen2.5:3b").check_readiness()

    assert readiness.api_reachable is True
    assert readiness.model_available is False
