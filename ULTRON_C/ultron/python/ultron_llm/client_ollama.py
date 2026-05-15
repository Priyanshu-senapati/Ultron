"""
client_ollama.py — Ollama /api/chat client with streaming support.

Streaming yields text chunks as they arrive (for voice responsiveness).
Non-streaming returns the full response string.
Tool calls in the response are detected but NOT stripped here — caller decides.
"""
from __future__ import annotations

import json
import logging
from typing import AsyncIterator, Optional

import httpx

logger = logging.getLogger("ultron.llm.ollama")


class OllamaClient:
    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        default_model: str = "llama3.2:3b",
        request_timeout: float = 120.0,
    ) -> None:
        self._url = base_url.rstrip("/")
        self._model = default_model
        self._timeout = request_timeout

    async def chat_stream(
        self,
        system_prompt: str,
        messages: list[dict],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 1024,
    ) -> AsyncIterator[str]:
        """
        Stream a chat response. Yields text chunks as they arrive.
        Final chunk may be an empty string (stream end marker).
        Raises httpx.HTTPError on connection failure — caller handles retry.
        """
        payload = {
            "model": model or self._model,
            "messages": [{"role": "system", "content": system_prompt}] + messages,
            "stream": True,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            async with client.stream(
                "POST", f"{self._url}/api/chat", json=payload
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    chunk = obj.get("message", {}).get("content", "")
                    if chunk:
                        yield chunk
                    if obj.get("done", False):
                        break

    async def chat(
        self,
        system_prompt: str,
        messages: list[dict],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> str:
        """Non-streaming chat. Returns the full response string."""
        chunks: list[str] = []
        async for chunk in self.chat_stream(
            system_prompt, messages, model, temperature, max_tokens
        ):
            chunks.append(chunk)
        return "".join(chunks)

    async def chat_with_images(
        self,
        system_prompt: str,
        user_text: str,
        images_b64: list[str],
        model: str = "llava:latest",
        temperature: float = 0.4,
        max_tokens: int = 1024,
    ) -> str:
        """Ollama vision chat — sends one user message with attached images."""
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text, "images": images_b64},
            ],
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(f"{self._url}/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data.get("message", {}).get("content", "")

    async def is_available(self) -> bool:
        """Quick health check — True if Ollama is reachable."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self._url}/api/tags")
                return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def list_models(self) -> list[str]:
        """Return names of available local models."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{self._url}/api/tags")
                resp.raise_for_status()
                return [m["name"] for m in resp.json().get("models", [])]
        except (httpx.HTTPError, KeyError):
            return []
