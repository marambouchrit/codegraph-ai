"""LLMProvider for Anthropic's Claude models (official `anthropic` SDK).

The only module that imports the SDK. It is imported lazily by `create_llm_provider()`,
so the API, retrieval and their tests start without it.

Request: one Messages API call, the instructions as `system`, the question and the
repository context as the single user message. Thinking is adaptive (always on for
current models); LLM_EFFORT sets how much. On a safety decline the API re-runs the
request on a fallback model (`fallbacks="default"`); if every model declines, the
answer is refused and reported as such, never invented.

Errors: SDK exceptions become LLMConfigurationError (key, permissions, model, rejected
request), LLMUnavailableError (network, timeout, rate limit, overloaded / 5xx) or
LLMResponseError (declined, empty, malformed). Messages and logs never contain the API
key, headers or prompt text: only the error type, HTTP status and request ID.
"""

import logging
from typing import Any, Self

import anthropic

from app.core.config import Settings
from app.core.errors import (
    LLMConfigurationError,
    LLMError,
    LLMResponseError,
    LLMUnavailableError,
)
from app.llm.models import LLMCompletion
from app.llm.provider import LLMProvider

logger = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider(LLMProvider):
    def __init__(
        self,
        client: Any,
        model: str,
        max_tokens: int = 16000,
        effort: str = "",
        temperature: float | None = None,
    ) -> None:
        self.client = client
        self._model = model
        self.max_tokens = max_tokens
        self.effort = effort.strip()
        self.temperature = temperature

    @classmethod
    def from_settings(cls, settings: Settings, api_key: str, model: str) -> Self:
        client = anthropic.Anthropic(
            api_key=api_key,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
        return cls(
            client,
            model,
            max_tokens=settings.llm_max_tokens,
            effort=settings.llm_effort,
            temperature=settings.llm_temperature,
        )

    @property
    def model(self) -> str:
        return self._model

    def generate(self, system_prompt: str, user_prompt: str) -> LLMCompletion:
        request: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self.max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
            "betas": [FALLBACK_BETA],
            "fallbacks": "default",
        }
        if self.effort:
            request["output_config"] = {"effort": self.effort}
        if self.temperature is not None:
            # Not a typed parameter any more (current models reject it): sent as is.
            request["extra_body"] = {"temperature": self.temperature}
        try:
            response = self.client.beta.messages.create(**request)
        except anthropic.APIError as error:
            raise _translate(error) from error
        except anthropic.AnthropicError as error:  # client-side: credentials, options...
            logger.error("LLM client error: %s", type(error).__name__)
            raise LLMConfigurationError(
                "The LLM client could not send the request: check the LLM_* settings."
            ) from error
        return _completion(response, self._model)


def _completion(response: Any, requested_model: str) -> LLMCompletion:
    try:
        stop_reason = response.stop_reason
        texts = [block.text for block in response.content if block.type == "text"]
        model = getattr(response, "model", None) or requested_model
        usage = getattr(response, "usage", None)
    except (AttributeError, TypeError) as error:
        logger.error("Malformed LLM response: %s", type(error).__name__)
        raise LLMResponseError("The LLM returned a malformed response.") from error
    if stop_reason == "refusal":
        raise LLMResponseError("The LLM declined to answer this question.")
    text = "\n".join(t for t in texts if isinstance(t, str)).strip()
    if not text:
        raise LLMResponseError("The LLM returned an empty answer.")
    return LLMCompletion(
        text=text,
        model=str(model),
        truncated=stop_reason == "max_tokens",
        input_tokens=getattr(usage, "input_tokens", None),
        output_tokens=getattr(usage, "output_tokens", None),
    )


def _translate(error: anthropic.APIError) -> LLMError:
    """An application error for an SDK error, with a message safe to show and log."""
    status = getattr(error, "status_code", None)
    request_id = getattr(error, "request_id", None)
    logger.error("LLM request failed: %s (HTTP %s, request %s)",
                 type(error).__name__, status, request_id)  # fmt: skip
    if isinstance(error, anthropic.AuthenticationError):
        return LLMConfigurationError("The LLM provider rejected the API key: check LLM_API_KEY.")
    if isinstance(error, anthropic.PermissionDeniedError):
        return LLMConfigurationError(
            "The API key is not allowed to use this model: check LLM_API_KEY and LLM_MODEL."
        )
    if isinstance(error, anthropic.NotFoundError):
        return LLMConfigurationError("Unknown LLM model: check LLM_MODEL.")
    if isinstance(error, anthropic.RateLimitError):
        return LLMUnavailableError("The LLM provider is rate limiting requests: retry later.")
    if isinstance(error, anthropic.BadRequestError):
        return LLMConfigurationError(
            "The LLM provider rejected the request (HTTP 400): check LLM_MODEL, LLM_EFFORT "
            "and LLM_TEMPERATURE (current Claude models accept no temperature)."
        )
    if isinstance(error, anthropic.APITimeoutError):  # before APIConnectionError: a subclass
        return LLMUnavailableError("The LLM did not answer in time (LLM_TIMEOUT_SECONDS).")
    if isinstance(error, anthropic.APIConnectionError):
        return LLMUnavailableError("The LLM provider cannot be reached: check the network.")
    if isinstance(error, anthropic.APIStatusError) and error.status_code >= 500:
        return LLMUnavailableError(
            f"The LLM provider is unavailable (HTTP {error.status_code}): retry later."
        )
    return LLMError("The LLM request failed.")
