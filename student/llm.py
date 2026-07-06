import os
import openai
from openai import OpenAI
from typing import Any, Dict, List

class TokenRotator:
    """Gère la rotation automatique multi-clés et multi-providers de manière abstraite."""

    def __init__(self) -> None:
        self.configs: List[Dict[str, str]] = []
        self.current_index = 0
        self._load_providers()

    def _load_providers(self) -> None:
        """Détecte dynamiquement les providers configurés dans le .env."""
        providers = ["GROQ", "OPENROUTER", "GEMINI", "OPENAI"]
        
        for p in providers:
            url = os.getenv(f"{p}_API_URL")
            model = os.getenv(f"{p}_MODEL_NAME")
            keys_raw = os.getenv(f"{p}_KEYS")
            
            if url and model and keys_raw:
                keys = [k.strip() for k in keys_raw.split(",") if k.strip()]
                for key in keys:
                    if p == "GEMINI" and model == "gemini-1.5-flash":
                        model = "gemini-2.5-flash"
                        
                    self.configs.append({
                        "url": url,
                        "model": model,
                        "key": key
                    })
                    
        if not self.configs:
            self.configs.append({
                "url": "https://api.groq.com/openai/v1",
                "model": "llama-3.3-70b-versatile",
                "key": "dummy"
            })

    def get_current_config(self) -> Dict[str, str]:
        """Retourne la configuration active (URL, Clé, Modèle)."""
        return self.configs[self.current_index]

    def rotate(self) -> None:
        """Passe à la clé suivante (ou au provider suivant) en cas de fail."""
        if len(self.configs) > 1:
            self.current_index = (self.current_index + 1) % len(self.configs)


class LLMClient:
    """Client OpenAI-compatible avec tolérance aux pannes et abstraction des providers."""

    def __init__(self, rotator: TokenRotator, provider_url: str, model_name: str) -> None:
        self.rotator = rotator
        self.cli_url = provider_url
        self.cli_model = model_name

    def call_api(self, messages: List[Dict[str, str]], tools: Any = None) -> Dict[str, Any]:
        """Exécute l'appel API en testant les configurations du rotator jusqu'à épuisement."""
        
        for _ in range(len(self.rotator.configs)):
            config = self.rotator.get_current_config()
            
            is_cli_compatible = True
            if self.cli_url and "groq" in self.cli_url.lower() and config["key"].startswith("sk-or"):
                is_cli_compatible = False
            elif self.cli_url and "openrouter" in self.cli_url.lower() and config["key"].startswith("gsk_"):
                is_cli_compatible = False
            elif config["key"].startswith("AIza"):
                is_cli_compatible = False

            if self.rotator.current_index == 0 and is_cli_compatible:
                base_url = self.cli_url if self.cli_url else config["url"]
                model_to_use = self.cli_model if self.cli_model else config["model"]
            else:
                base_url = config["url"]
                model_to_use = config["model"]
            
            client = OpenAI(base_url=base_url, api_key=config["key"])
            
            try:
                # payload strict optimisé pour le code sans hallucination créative
                kwargs = {
                    "model": model_to_use, 
                    "messages": messages,
                    "temperature": 0.0,
                    "top_p": 0.95,
                    "max_tokens": 4096
                }
                if tools:
                    kwargs["tools"] = tools
                    
                response = client.chat.completions.create(**kwargs)
                
                return {
                    "text": response.choices[0].message.content or "",
                    "tool_calls": getattr(response.choices[0].message, "tool_calls", None),
                    "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
                    "completion_tokens": response.usage.completion_tokens if response.usage else 0,
                }
                
            except (openai.RateLimitError, openai.OpenAIError) as e:
                print(f"\n⚠️ Échec avec le Provider {base_url} (Modèle: {model_to_use}) : {e}")
                print("🔄 Rotation automatique vers la configuration suivante...")
                self.rotator.rotate()

        raise RuntimeError("LLM_API_EXHAUSTED: Toutes les clés de tous les providers ont échoué ou sont saturées.")