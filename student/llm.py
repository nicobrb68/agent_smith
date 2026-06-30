import openai
from openai import OpenAI
from typing import Any, Dict
import sys


class TokenRotator:
    """Manages API token rotation to handle rate limits and quotas."""

    def __init__(self, keys: list[str]) -> None:
        """Initialize the rotator with a list of API keys."""
        self.api_key = keys
        self.current_index = 0

    def get_current_key(self) -> str:
        """Return the active API key."""
        return self.api_key[self.current_index]

    def rotate_key(self) -> None:
        """Switch to the next available API key."""
        self.current_index = (self.current_index + 1) % len(self.api_key)


class LLMClient:
    """Handles OpenAI-compatible API calls with integrated token rotation."""

    def __init__(
        self, rotator: TokenRotator, provider_url: str, model_name: str
    ) -> None:
        """Initialize the client with a rotator and provider configuration."""
        self.rotator = rotator
        self.provider_url = provider_url
        self.model_name = model_name

    def call_api(
        self, 
        messages: list[dict[str, str]], 
        tools: list[dict[str, Any]] | None = None
    ) -> Dict[str, Any]:
        """Execute API call with fallback key rotation on rate limits."""
        for _ in range(len(self.rotator.api_key)):
            client = OpenAI(
                base_url=self.provider_url,
                api_key=self.rotator.get_current_key(),
            )
            try:
                kwargs = {
                    "model": self.model_name,
                    "messages": messages
                }
                if tools:
                    kwargs["tools"] = tools
                response = client.chat.completions.create(**kwargs)

                message = response.choices[0].message
                answer_text = message.content or ""
                tool_calls = message.tool_calls if hasattr(message, "tool_calls") else None

                prompt_tokens = (
                    response.usage.prompt_tokens if response.usage else 0
                )
                completion_tokens = (
                    response.usage.completion_tokens if response.usage else 0
                )

                return {
                    "text": answer_text,
                    "tool_calls": tool_calls,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                }

            except (openai.RateLimitError, openai.OpenAIError) as e:
                current_key = self.rotator.get_current_key()
                print(
                    f"Token error on {current_key}: {e}\n"
                    "Trying another one..."
                )
                self.rotator.rotate_key()

        # Raised if all available tokens in the loop failed
        print("All provided API tokens have failed or exhausted.\nEnd of program.")
        sys.exit(1)
