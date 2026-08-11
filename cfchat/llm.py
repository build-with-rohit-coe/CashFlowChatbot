"""One interface, two backends: Gemini (hosted) and Qwen2.5:7b (local via Ollama).

Both return plain text. JSON coercion lives here too, because a 7B model will
occasionally wrap its JSON in ```json fences no matter how firmly you ask it not to.
"""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod

import requests

from .config import LLM, LLMConfig


class LLMError(RuntimeError):
    """Model produced something unusable."""


class LLMUnavailable(LLMError):
    """Couldn't reach the model at all - a setup problem, not a bad question."""


class Backend(ABC):
    name: str

    @abstractmethod
    def complete(self, system: str, user: str, json_mode: bool = False) -> str: ...


class GeminiBackend(Backend):
    name = "gemini"

    def __init__(self, cfg: LLMConfig):
        if not cfg.gemini_api_key:
            raise LLMUnavailable("GEMINI_API_KEY is not set.")
        from google import genai                      # google-genai
        from google.genai import types

        self._types = types
        self.client = genai.Client(api_key=cfg.gemini_api_key)
        self.model = cfg.gemini_model
        self.temperature = cfg.temperature

    def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        cfg = self._types.GenerateContentConfig(
            system_instruction=system,
            temperature=self.temperature,
            response_mime_type="application/json" if json_mode else "text/plain",
        )
        resp = self.client.models.generate_content(
            model=self.model, contents=user, config=cfg
        )
        text = (resp.text or "").strip()
        if not text:
            raise LLMError("Gemini returned an empty response.")
        return text


class OllamaBackend(Backend):
    name = "qwen"

    def __init__(self, cfg: LLMConfig):
        self.host = cfg.ollama_host.rstrip("/")
        self.model = cfg.ollama_model
        self.temperature = cfg.temperature

    def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {
                "temperature": self.temperature,
                "num_ctx": 8192,        # the schema card alone is ~1.5k tokens
                "top_p": 0.9,
            },
        }
        if json_mode:
            payload["format"] = "json"
        try:
            resp = requests.post(f"{self.host}/api/chat", json=payload, timeout=180)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise LLMUnavailable(
                f"Could not reach Ollama at {self.host}. Is `ollama serve` running "
                f"and `ollama pull {self.model}` done? ({exc})"
            ) from exc
        return resp.json().get("message", {}).get("content", "").strip()


def get_backend(cfg: LLMConfig | None = None) -> Backend:
    cfg = cfg or LLM
    provider = cfg.provider.lower()
    if provider in ("gemini", "google"):
        return GeminiBackend(cfg)
    if provider in ("qwen", "ollama", "local"):
        return OllamaBackend(cfg)
    raise LLMError(f"Unknown provider '{cfg.provider}'. Use 'gemini' or 'qwen'.")


_FENCE = re.compile(r"^```(?:json|sql)?\s*|\s*```$", re.MULTILINE)


def parse_json(text: str) -> dict:
    """Best-effort JSON extraction from a model response."""
    cleaned = _FENCE.sub("", text).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Fall back to the outermost {...} block.
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise LLMError(f"Model did not return valid JSON:\n{text[:500]}")


def strip_sql_fence(text: str) -> str:
    return _FENCE.sub("", text).strip().rstrip(";")
