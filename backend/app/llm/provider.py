"""How we talk to an LLM: one small interface, one implementation per API protocol.

    LLMProvider.generate(system_prompt, user_prompt) -> LLMCompletion

The rest of the application only knows this interface. Two implementations:

    anthropic_provider.py           Claude (Anthropic's Messages API)
    openai_compatible_provider.py   any OpenAI-compatible Chat Completions API: Gemini,
                                    Groq, OpenRouter (all with a free tier), or another
                                    server given by LLM_BASE_URL (a local one, for example)

`create_llm_provider()` picks one from LLM_PROVIDER, with a preset base URL, default
model and API key variable per provider. Adding a provider is a preset line (same
protocol) or one new class. Providers raise the application's LLM*Errors, never SDK errors.
"""

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.core.config import Settings
from app.core.errors import LLMConfigurationError
from app.llm.models import LLMCompletion


class LLMProvider(ABC):
    @property
    @abstractmethod
    def model(self) -> str: ...

    @abstractmethod
    def generate(self, system_prompt: str, user_prompt: str) -> LLMCompletion:
        """One completion: `system_prompt` holds the instructions, `user_prompt` the data.

        Raises LLMConfigurationError, LLMUnavailableError or LLMResponseError.
        """


@dataclass(frozen=True)
class ProviderPreset:
    protocol: str  # "anthropic" or "openai"
    base_url: str  # "" : the SDK's default (Anthropic) or LLM_BASE_URL required
    default_model: str  # "" : LLM_MODEL required
    key_variable: str  # read when LLM_API_KEY is empty ("" : none)


# Free tiers and model names change: check the provider's console (see docs/architecture.md).
# Gemini uses Google's moving "latest Flash-Lite" alias: pinned versions get retired for new
# users (gemini-2.5-flash was, in 2026), and the larger Flash models often answer 503 ("high
# demand") on the free tier. Set LLM_MODEL for another one (e.g. gemini-flash-latest).
PRESETS: dict[str, ProviderPreset] = {
    "gemini": ProviderPreset(
        "openai", "https://generativelanguage.googleapis.com/v1beta/openai/",
        "gemini-flash-lite-latest", "GEMINI_API_KEY",
    ),
    "groq": ProviderPreset(
        "openai", "https://api.groq.com/openai/v1", "openai/gpt-oss-120b", "GROQ_API_KEY"
    ),
    "openrouter": ProviderPreset("openai", "https://openrouter.ai/api/v1", "", "OPENROUTER_API_KEY"),
    "openai_compatible": ProviderPreset("openai", "", "", ""),
    "anthropic": ProviderPreset("anthropic", "", "claude-opus-5-5", "ANTHROPIC_API_KEY"),
}  # fmt: skip


def create_llm_provider(settings: Settings) -> LLMProvider:
    """The provider selected by LLM_PROVIDER, configured from the settings."""
    name = settings.llm_provider.strip().lower()
    preset = PRESETS.get(name)
    if preset is None:
        raise LLMConfigurationError(
            f"Unsupported LLM_PROVIDER '{settings.llm_provider}' "
            f"(supported: {', '.join(PRESETS)})."
        )
    _check_settings(settings)
    model = settings.llm_model.strip() or preset.default_model
    if not model:
        raise LLMConfigurationError(f"LLM_MODEL is required with LLM_PROVIDER={name}.")
    base_url = settings.llm_base_url.strip() or preset.base_url
    if preset.protocol == "openai" and not base_url:
        raise LLMConfigurationError(f"LLM_BASE_URL is required with LLM_PROVIDER={name}.")
    api_key = settings.llm_api_key.get_secret_value().strip()
    if not api_key and preset.key_variable:
        api_key = os.environ.get(preset.key_variable, "").strip()
    if not api_key:
        where = f"LLM_API_KEY (or {preset.key_variable})" if preset.key_variable else "LLM_API_KEY"
        raise LLMConfigurationError(f"No API key for the LLM: set {where} in backend/.env.")

    # Imported here: they load a provider SDK, which only answer generation needs.
    if preset.protocol == "anthropic":
        from app.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider.from_settings(settings, api_key, model)
    from app.llm.openai_compatible_provider import OpenAICompatibleProvider

    return OpenAICompatibleProvider.create(
        provider_name=name, base_url=base_url, api_key=api_key, model=model,
        max_tokens=settings.llm_max_tokens, temperature=settings.llm_temperature,
        timeout=settings.llm_timeout_seconds, max_retries=settings.llm_max_retries,
    )  # fmt: skip


def _check_settings(settings: Settings) -> None:
    if settings.llm_max_tokens < 1:
        raise LLMConfigurationError("LLM_MAX_TOKENS must be at least 1.")
    if settings.llm_timeout_seconds <= 0:
        raise LLMConfigurationError("LLM_TIMEOUT_SECONDS must be positive.")
    if settings.llm_max_retries < 0:
        raise LLMConfigurationError("LLM_MAX_RETRIES must not be negative.")
    temperature = settings.llm_temperature
    if temperature is not None and not 0.0 <= temperature <= 2.0:
        raise LLMConfigurationError("LLM_TEMPERATURE must be from 0 to 2 (or unset).")
