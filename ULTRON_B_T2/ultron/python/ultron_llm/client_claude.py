"""
client_claude.py — Claude API client (fallback for complex reasoning).

Only used when:
  - Local Ollama is unavailable, OR
  - The request is classified as high-complexity (> threshold)

Requires: pip install anthropic
API key: config.toml [llm] claude_api_key = "..." or ANTHROPIC_API_KEY env var.
"""
from __future__ import annotations

import asyncio
import logging
import queue as _queue
from typing import AsyncIterator, Optional

logger = logging.getLogger("ultron.llm.claude")


class ClaudeClient:
    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-20250514",
        max_tokens: int = 2048,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._max_tokens = max_tokens
        self._client: Optional[object] = None

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic
                self._client = anthropic.Anthropic(api_key=self._api_key)
            except ImportError:
                raise RuntimeError(
                    "anthropic package not installed. Run: pip install anthropic"
                )
        return self._client

    def is_configured(self) -> bool:
        """True if an API key is set."""
        return bool(self._api_key)

    async def chat(
        self,
        system_prompt: str,
        messages: list[dict],
        temperature: float = 0.7,
    ) -> str:
        """
        Non-streaming Claude chat. Runs sync client in thread pool to
        avoid blocking the event loop (anthropic SDK is synchronous).
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            self._sync_chat,
            system_prompt,
            messages,
            temperature,
        )

    def _sync_chat(
        self,
        system_prompt: str,
        messages: list[dict],
        temperature: float,
    ) -> str:
        client = self._get_client()
        try:
            resp = client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=system_prompt,
                messages=messages,
                temperature=temperature,
            )
            return resp.content[0].text if resp.content else ""
        except Exception as exc:
            logger.error("Claude API error: %s", exc)
            raise

    async def chat_stream(
        self,
        system_prompt: str,
        messages: list[dict],
        temperature: float = 0.7,
    ) -> AsyncIterator[str]:
        """
        Streaming Claude chat. Yields text chunks.
        Uses streaming API via thread-based adapter.
        """
        q: _queue.Queue[Optional[str]] = _queue.Queue()

        def _worker():
            try:
                client = self._get_client()
                with client.messages.stream(
                    model=self._model,
                    max_tokens=self._max_tokens,
                    system=system_prompt,
                    messages=messages,
                    temperature=temperature,
                ) as stream:
                    for text in stream.text_stream:
                        q.put(text)
            except Exception as exc:
                logger.error("Claude stream error: %s", exc)
            finally:
                q.put(None)  # sentinel

        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, _worker)

        while True:
            chunk = await loop.run_in_executor(None, q.get)
            if chunk is None:
                break
            yield chunk
