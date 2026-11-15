"""Ollama client (R2). The only LLM runtime; the host is a config value."""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

import httpx


class ContextOverflow(RuntimeError):
    """Raised *before* a call whose prompt cannot fit. R9 forbids context
    overflow; Ollama silently truncates the front of an over-long prompt,
    which would drop transcript without any error, so we refuse instead."""


def estimate_tokens(text: str) -> int:
    # Rough English heuristic (~0.75 words/token, plus punctuation and line
    # ids). Deliberately pessimistic: overestimating only costs an extra
    # chunk, underestimating silently truncates. Unvalidated against the
    # Gemma/Qwen tokenizers; M0 should compare with Ollama's prompt_eval_count.
    words = len(text.split())
    return int(words * 1.45) + text.count("\n") + 16


class LLM(Protocol):
    model: str
    context: int

    def chat_json(self, system: str, user: str, schema: dict, max_tokens: int, temperature: float = 0.1) -> Any: ...


class OllamaLLM:
    def __init__(self, host: str, model: str, context: int, num_gpu: int | None = None, timeout: float = 1800):
        self.host, self.model, self.context, self.num_gpu = host.rstrip("/"), model, context, num_gpu
        self.client = httpx.Client(timeout=timeout, trust_env=False)  # never route via a proxy

    def _check(self, system: str, user: str, max_tokens: int) -> None:
        need = estimate_tokens(system) + estimate_tokens(user) + max_tokens
        if need > self.context:
            raise ContextOverflow(f"prompt needs ~{need} tokens, context is {self.context}")

    def chat_json(self, system: str, user: str, schema: dict, max_tokens: int, temperature: float = 0.1) -> Any:
        self._check(system, user, max_tokens)
        options: dict[str, Any] = {"temperature": temperature, "num_ctx": self.context, "num_predict": max_tokens}
        if self.num_gpu is not None:
            options["num_gpu"] = self.num_gpu
        r = self.client.post(f"{self.host}/api/chat", json={
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "format": schema,  # Ollama structured outputs: constrains decoding to the schema
            "stream": False,
            "options": options,
        })
        r.raise_for_status()
        return parse_json(r.json()["message"]["content"])

    def unload(self) -> None:
        try:
            self.client.post(f"{self.host}/api/generate", json={"model": self.model, "keep_alive": 0})
        except httpx.HTTPError:
            pass

    def list_models(self) -> list[str]:
        r = self.client.get(f"{self.host}/api/tags", timeout=5)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]


def parse_json(text: str) -> Any:
    text = text.strip()
    # Some models wrap JSON in a fence or emit a <think> block despite format.
    text = re.sub(r"^<think>.*?</think>", "", text, flags=re.S).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise
