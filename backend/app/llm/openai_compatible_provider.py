"""LLMProvider for any "OpenAI-compatible" chat API (official `openai` SDK).

Many providers expose the same Chat Completions protocol at their own URL, several with
a free tier: Google Gemini, Groq, OpenRouter... One class serves them all; only the base
URL, the key and the model change (presets in provider.py). Imported lazily by
`create_llm_provider()`, like the Anthropic provider.

Request: one chat completion, the instructions as the "system" message and the question
with the repository context as the "user" message. Errors: SDK exceptions become
LLMConfigurationError / LLMUnavailableError / LLMResponseError, and messages and logs
never contain the API key, headers or prompt text.
"""

import logging
from typing import Any

import openai

from app.core.errors import (
    LLMConfigurationError,
    LLMError,
    LLMResponseError,
    LLMUnavailableError,
)
from app.llm.models import LLMCompletion
from app.llm.provider import LLMProvider

logger = logging.getLogger(__name__)


class OpenAICompatibleProvider(LLMProvider):
    def __init__(
        self,
        client: Any,
        model: str,
        provider_name: str = "openai_compatible",
        max_tokens: int = 16000,
        temperature: float | None = None,
    ) -> None:
        self.client = client
        self._model = model
        self.provider_name = provider_name
        self.max_tokens = max_tokens
        self.temperature = temperature

    @classmethod
    def create(
        cls, *, provider_name: str, base_url: str, api_key: str, model: str, max_tokens: int,
        temperature: float | None, timeout: float, max_retries: int,
    ) -> "OpenAICompatibleProvider":  # fmt: skip
        client = openai.OpenAI(
            api_key=api_key, base_url=base_url, timeout=timeout, max_retries=max_retries
        )
        return cls(client, model, provider_name, max_tokens, temperature)

    @property
    def model(self) -> str:
        return self._model

    def generate(self, system_prompt: str, user_prompt: str) -> LLMCompletion:
        request: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if self.temperature is not None:
            request["temperature"] = self.temperature
        try:
            response = self.client.chat.completions.create(**request)
        except openai.APIError as error:
            raise _translate(error, self.provider_name) from error
        except openai.OpenAIError as error:  # client-side: options...
            logger.error("LLM client error: %s", type(error).__name__)
            raise LLMConfigurationError(
                "The LLM client could not send the request: check the LLM_* settings."
            ) from error
        return _completion(response, self._model)


def _completion(response: Any, requested_model: str) -> LLMCompletion:
    try:
        choice = response.choices[0]
        finish_reason = choice.finish_reason
        text = choice.message.content
        model = getattr(response, "model", None) or requested_model
        usage = getattr(response, "usage", None)
    except (AttributeError, TypeError, IndexError) as error:
        logger.error("Malformed LLM response: %s", type(error).__name__)
        raise LLMResponseError("The LLM returned a malformed response.") from error
    if finish_reason == "content_filter":
        raise LLMResponseError("The LLM declined to answer this question.")
    if not isinstance(text, str) or not text.strip():
        raise LLMResponseError("The LLM returned an empty answer.")
    return LLMCompletion(
        text=text.strip(),
        model=str(model),
        truncated=finish_reason == "length",
        input_tokens=getattr(usage, "prompt_tokens", None),
        output_tokens=getattr(usage, "completion_tokens", None),
    )


def _translate(error: openai.APIError, provider: str) -> LLMError:
    """An application error for an SDK error, with a message safe to show and log."""
    status = getattr(error, "status_code", None)
    request_id = getattr(error, "request_id", None)
    logger.error("LLM request to %s failed: %s (HTTP %s, request %s)",
                 provider, type(error).__name__, status, request_id)  # fmt: skip
    if isinstance(error, openai.AuthenticationError):
        return LLMConfigurationError(
            f"The LLM provider ({provider}) rejected the API key: check LLM_API_KEY."
        )
    if isinstance(error, openai.PermissionDeniedError):
        return LLMConfigurationError(
            "The API key is not allowed to use this model: check LLM_API_KEY and LLM_MODEL."
        )
    if isinstance(error, openai.NotFoundError):
        return LLMConfigurationError("Unknown LLM model or endpoint: check LLM_MODEL and LLM_BASE_URL.")
    if isinstance(error, openai.RateLimitError):
        return LLMUnavailableError(
            f"The LLM provider ({provider}) is rate limiting requests (free tier quota?): "
            "retry later."
        )
    if isinstance(error, openai.BadRequestError):
        return LLMConfigurationError(
            "The LLM provider rejected the request (HTTP 400): check LLM_MODEL, "
            "LLM_MAX_TOKENS and LLM_TEMPERATURE."
        )
    if isinstance(error, openai.APITimeoutError):  # before APIConnectionError: a subclass
        return LLMUnavailableError("The LLM did not answer in time (LLM_TIMEOUT_SECONDS).")
    if isinstance(error, openai.APIConnectionError):
        return LLMUnavailableError("The LLM provider cannot be reached: check the network.")
    if isinstance(error, openai.APIStatusError) and error.status_code >= 500:
        return LLMUnavailableError(
            f"The LLM provider is unavailable (HTTP {error.status_code}): retry later."
        )
    if isinstance(error, openai.APIStatusError) and error.status_code == 413:
        return LLMConfigurationError(
            "The prompt is too large for this provider's limits (HTTP 413): use another "
            "model or provider, or lower LLM_MAX_TOKENS."
        )
    return LLMError("The LLM request failed.")
