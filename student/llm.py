"""OpenAI-compatible LLM client with multi-provider key rotation.

TokenRotator discovers provider configurations (URL, model, API
keys) from environment variables, and LLMClient wraps the OpenAI
SDK to call whichever provider is currently active, rotating to the
next key/provider on rate limits or transient API errors.
"""
import os
from typing import Any, Dict, List, Optional

import openai
from openai import OpenAI

DEFAULT_TEMPERATURE: float = 0.0
DEFAULT_TOP_P: float = 0.95
DEFAULT_MAX_TOKENS: int = 4096


class TokenRotator:
    """Handle automatic multi-key, multi-provider rotation."""

    def __init__(self) -> None:
        """Load every configured provider from the environment."""
        self.configs: List[Dict[str, str]] = []
        self.current_index: int = 0
        self._load_providers()

    def _load_providers(self) -> None:
        """Discover provider configurations from the .env file."""
        providers = [
            "GROQ", "OPENROUTER", "GEMINI",
            "OPENAI", "MISTRAL",
        ]

        for p in providers:
            url = os.getenv(f"{p}_API_URL")
            model = os.getenv(f"{p}_MODEL_NAME")
            keys_raw = os.getenv(f"{p}_KEYS")

            if url and model and keys_raw:
                keys = [
                    k.strip() for k in keys_raw.split(",") if k.strip()
                ]
                for key in keys:
                    if p == "GEMINI" and model == "gemini-1.5-flash":
                        model = "gemini-2.5-flash"

                    self.configs.append(
                        {"url": url, "model": model, "key": key}
                    )

        if not self.configs:
            self.configs.append(
                {
                    "url": "https://api.groq.com/openai/v1",
                    "model": "llama-3.3-70b-versatile",
                    "key": "dummy",
                }
            )

    def get_current_config(self) -> Dict[str, str]:
        """Return the currently active configuration.

        Returns:
            Dict[str, str]: A mapping with ``url``, ``model`` and
            ``key`` for the active provider/key pair.
        """
        return self.configs[self.current_index]

    def rotate(self) -> None:
        """Move to the next key/provider after a failure."""
        if len(self.configs) > 1:
            self.current_index = (
                self.current_index + 1
            ) % len(self.configs)


class LLMClient:
    """OpenAI-compatible client with provider fallback.

    The sampling ``temperature`` is configurable per instance (and
    can be overridden per call) instead of being hardcoded. A
    deterministic model (temperature 0.0) is a good fit for an
    agent that mostly needs to follow a strict tool-use protocol
    (e.g. SWE-bench), but it can trap a self-correcting agent in a
    loop where it keeps regenerating the exact same wrong answer
    (e.g. MBPP) since nothing forces it to explore an alternative.
    """

    def __init__(
        self,
        rotator: TokenRotator,
        provider_url: str,
        model_name: str,
        temperature: float = DEFAULT_TEMPERATURE,
        top_p: float = DEFAULT_TOP_P,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        """Store the rotator and the default sampling settings.

        Args:
            rotator: The TokenRotator used for provider fallback.
            provider_url: Preferred base URL (CLI-provided).
            model_name: Preferred model name (CLI-provided).
            temperature: Default sampling temperature for calls
                that do not override it explicitly.
            top_p: Default nucleus-sampling value.
            max_tokens: Default max tokens per completion.
        """
        self.rotator = rotator
        self.cli_url = provider_url
        self.cli_model = model_name
        self.temperature = temperature
        self.top_p = top_p
        self.max_tokens = max_tokens

    def call_api(
        self,
        messages: List[Dict[str, str]],
        tools: Any = None,
        temperature: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Call the LLM API, rotating providers on failure.

        Args:
            messages: The conversation to send to the model.
            tools: Optional tool schema forwarded as-is to the API.
            temperature: Optional per-call override of the
                instance's default temperature.

        Returns:
            Dict[str, Any]: The generated text, tool calls (if
            any), and prompt/completion token counts.

        Raises:
            RuntimeError: If every configured provider/key has
                failed or is exhausted.
        """
        effective_temperature = (
            self.temperature if temperature is None else temperature
        )

        for _ in range(len(self.rotator.configs)):
            config = self.rotator.get_current_config()

            key = config["key"]
            cli_matches_provider = (
                self.cli_url
                and config["url"]
                and self.cli_url.rstrip("/").lower()
                == config["url"].rstrip("/").lower()
            )

            if cli_matches_provider:
                base_url = self.cli_url
                model_to_use = (
                    self.cli_model or config["model"]
                )
            else:
                base_url = config["url"]
                model_to_use = config["model"]

            client = OpenAI(base_url=base_url, api_key=key)

            try:
                kwargs: Dict[str, Any] = {
                    "model": model_to_use,
                    "messages": messages,
                    "temperature": effective_temperature,
                    "top_p": self.top_p,
                    "max_tokens": self.max_tokens,
                }
                if tools:
                    kwargs["tools"] = tools

                response = client.chat.completions.create(**kwargs)

                return {
                    "text": response.choices[0].message.content or "",
                    "tool_calls": getattr(
                        response.choices[0].message, "tool_calls", None
                    ),
                    "prompt_tokens": (
                        response.usage.prompt_tokens
                        if response.usage
                        else 0
                    ),
                    "completion_tokens": (
                        response.usage.completion_tokens
                        if response.usage
                        else 0
                    ),
                }

            except (openai.RateLimitError, openai.OpenAIError) as e:
                print(
                    f"\nProvider failure {base_url} "
                    f"(model: {model_to_use}): {e}"
                )
                print("Rotating to the next configuration...")
                self.rotator.rotate()

        raise RuntimeError(
            "LLM_API_EXHAUSTED: every provider/key configured has "
            "failed or is rate-limited."
        )
