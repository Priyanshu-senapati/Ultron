"""
service.py — LLMService: the brain of ULTRON.

Subscribes to the WS bus for live state.
Exposes ask() for B (Voice Engine) and future modules.
Routes between Ollama (local) and Claude API (complex fallback).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncIterator, Optional, Union

from ultron_bridge import UltronBridge

from .client_claude import ClaudeClient
from .client_ollama import OllamaClient
from .config import LLMConfig
from .context import ContextAssembler
from .conversation import ConversationHistory
from .intent_router import route as intent_route
from .personality import Shard, select_shard

import re as _re


# Heuristic: does the prompt look like it's referencing the past in a
# way that a last-N-turn window might not cover? If yes, the service
# kicks off a recall query before invoking the LLM. Cheap regex; if it
# triggers a false positive the recall block is just an extra ~150 chars
# of harmless context.
_PAST_REFERENCE_RE = _re.compile(
    r"\b(remember|recall|earlier|last\s+(?:time|week|month|day|night|"
    r"session)|previous|previously|you\s+(?:said|told|mentioned)|"
    r"we\s+(?:said|discussed|talked|agreed|decided|built|did)|"
    r"yesterday|the\s+other\s+(?:day|time)|before|ago|"
    r"what\s+(?:was|did\s+you\s+say)|"
    r"how\s+did\s+we|where\s+did\s+we|what\s+(?:is\s+)?my\s+"
    r"(?:dog|cat|kid|partner|wife|husband|son|daughter|friend|name|favourite|favorite))\b",
    _re.IGNORECASE,
)


def _looks_like_past_reference(prompt: str) -> bool:
    return bool(_PAST_REFERENCE_RE.search(prompt or ""))


# Patterns the local LLM keeps emitting against explicit prompt rules.
# Stripped before the response leaves the service — TTS and HUD never
# see them. Each is conservative: removes the offending fragment and
# any trailing comma/whitespace, leaves the rest of the sentence alone.

# "Commander Priyanshu" / "Sir Priyanshu" / variants. We replace the
# whole "<rank> <name>" with just the rank — the persona allows
# "Commander," / "Sir," but never "Commander Priyanshu".
_NAME_AFTER_TITLE = _re.compile(
    r"\b(sir|commander|chief|boss|captain)\s+([A-Z][a-zA-Z]+)",
)

# Surveillance preambles. Anchored to start-of-response or after a
# sentence break so we don't nuke a mid-sentence "I see…".
_SURVEILLANCE_PREAMBLES = [
    _re.compile(
        r"^(?:I\s+see\s+(?:that\s+)?|I\s+notice\s+(?:that\s+)?|"
        r"I\s+can\s+see\s+(?:that\s+)?|"
        r"Given\s+(?:that\s+)?your\s+current\s+state[,.]?\s*|"
        r"Based\s+on\s+your\s+(?:current\s+)?(?:state|context|telemetry|snapshot)[,.]?\s*|"
        r"Considering\s+your\s+(?:current\s+)?(?:state|request|context)[,.]?\s*|"
        r"(?:The\s+)?Claude\s+(?:Code\s+)?session\s+data\s+[^.]*\.\s*)",
        _re.IGNORECASE),
]


def _sanitize_persona(text: str, user_first_name: str) -> str:
    """Strip "Commander <FirstName>" + surveillance preambles + leading
    sycophantic boilerplate the local model loves to emit."""
    if not text:
        return text
    out = text

    # Strip "Commander Priyanshu" → "Commander". Defensive against any
    # first name the user might have configured.
    first = (user_first_name or "").strip()
    if first:
        out = _re.sub(
            rf"\b(sir|commander|chief|boss|captain)\s+{_re.escape(first)}\b",
            r"\1",
            out,
            flags=_re.IGNORECASE,
        )
    # Generic catch — anything that looks like "<title> <Capitalised>".
    # Skip common name-like words that follow titles legitimately.
    out = _NAME_AFTER_TITLE.sub(r"\1", out)

    # Trim surveillance preambles from the very start of the response.
    # Iterate until no more leading matches remain.
    for _ in range(3):
        before = out
        for pat in _SURVEILLANCE_PREAMBLES:
            out = pat.sub("", out, count=1).lstrip()
        if out == before:
            break
    # Capitalise the new leading character if we ate a preamble.
    out = out.strip()
    if out and out[0].islower():
        out = out[0].upper() + out[1:]
    return out
from .preference import PreferenceEngine, _load_band
from .state import LiveState
from .tool_parser import ToolCall, parse_tool_calls, strip_tool_calls
from .vision import VISION_SYSTEM_PROMPT, capture_screen_b64, looks_visual
from .web_search import format_results, looks_searchable, search as web_search

logger = logging.getLogger("ultron.llm.service")


class LLMService:
    def __init__(self, config: LLMConfig) -> None:
        self._cfg = config
        self._state = LiveState()
        # Persist conversation history to disk so restarts preserve context.
        # File lives next to memory.db / knowledge.db in the data dir.
        self._history = ConversationHistory(
            max_turns=config.max_history_turns,
            persist_path=config.memory_db_path.parent / "conversation.json",
        )
        self._ollama = OllamaClient(
            base_url=config.ollama_url,
            default_model=config.ollama_model,
        )
        self._claude = ClaudeClient(
            api_key=config.claude_api_key,
            model=config.claude_model,
        )
        # Knowledge retriever is optional — only wires up if the package
        # is importable AND the knowledge.db exists.
        retriever = None
        try:
            from ultron_knowledge import KnowledgeRetriever  # type: ignore[import-not-found]
            kg_db = config.memory_db_path.parent / "knowledge.db"
            if kg_db.exists():
                retriever = KnowledgeRetriever(db_path=kg_db)
                logger.info("knowledge retriever wired (db=%s)", kg_db)
        except ImportError:
            logger.debug("ultron_knowledge unavailable — retrieval disabled")
        self._assembler = ContextAssembler(
            memory_db_path=config.memory_db_path,
            max_context_memories=config.max_context_memories,
            high_load_threshold=config.high_load_threshold,
            user_name=config.user_name,
            retriever=retriever,
        )
        self._preference = PreferenceEngine(
            db_path=config.memory_db_path.parent / "preference.db"
        )
        self._bridge: Optional[UltronBridge] = None
        # Long-term recall integration: when the prompt looks like it
        # references past context, the service publishes a
        # recall_query_request and waits on the next recall_query_result
        # for the prompt block. FIFO queue keeps the future / response
        # pairing simple (this service handles one prompt at a time).
        self._recall_waiters: list[asyncio.Future] = []

    # ── Public API ────────────────────────────────────────────────────────

    async def ask(
        self,
        prompt: str,
        mode: str = "default",   # "default" | "voice"
        stream: bool = False,
    ) -> Union[str, AsyncIterator[str]]:
        """
        Primary entry point. B calls this. Returns full response string,
        or an async iterator of chunks if stream=True.

        mode="voice":
          - Short response (max 3 sentences via system prompt addendum)
          - Plain text, no markdown
          - Always uses Ollama (latency matters for voice)

        mode="default":
          - Full response
          - Routes to Claude if complexity is high and API key exists
        """
        # Notify preference engine of incoming message before mutating history.
        self._preference.on_user_message(prompt)

        # Hard intent router — the local model is unreliable at the
        # tool-call protocol and leaks persona violations even with
        # explicit rules. For unambiguous verbs ("play X", "open X",
        # "search X", "brightness N", "pause / next / mute") we bypass
        # the LLM entirely and dispatch the tool deterministically.
        # Pass live state so data-questions ("what's the weather",
        # "what time is it") can answer straight from cached state
        # without a round-trip too.
        intent = intent_route(prompt, self._state)
        if intent is not None:
            self._history.add_user(prompt)
            self._history.add_assistant(intent.reply)
            if intent.is_data_intent:
                # No tool to call — the reply *is* the answer.
                await self._publish_response(intent.reply, shard="intent")
                return intent.reply
            tool_call = ToolCall(
                name=intent.tool_name, args=intent.args,
                raw=f"intent-router:{intent.tool_name}",
            )
            await self._publish_tool_calls([tool_call])
            if intent.reply:
                await self._publish_response(intent.reply, shard="intent")
            return intent.reply

        # Visual question? Take a screenshot now and route through the
        # vision model. Real-time sight grounded in actual pixels.
        if looks_visual(prompt):
            visual = await self._visual_answer(prompt, mode)
            if visual is not None:
                self._history.add_user(prompt)
                self._history.add_assistant(visual)
                await self._publish_response(visual, shard="vision")
                return visual

        # Web search if the prompt is asking for current / external info.
        # We do this BEFORE the context assembly so results are part of
        # the prompt the model sees.
        web_results_block = ""
        if looks_searchable(prompt):
            try:
                results = await web_search(prompt, max_results=5)
                web_results_block = format_results(results)
                if web_results_block:
                    logger.info("web_search: %d results for %r", len(results), prompt[:80])
            except Exception as exc:  # noqa: BLE001
                logger.warning("web search failed: %s", exc)

        # Assemble context (uses the *prior* history; the new prompt isn't
        # added to _history until below).
        # If we have web results, prepend them to the user message so they
        # appear right before the question — the model is least likely to
        # ignore context that sits next to the prompt.
        effective_prompt = prompt
        if web_results_block:
            effective_prompt = f"{web_results_block}\n\n{prompt}"

        # Long-term recall — only fetch when the prompt looks like it
        # references the past. Saves an embed + cosine search on every
        # prompt, while still giving the model memory when it'd otherwise
        # fail ("what did you say about X", "remember when…", "the thing
        # we built last week"). The LLM can ALSO call the `recall` tool
        # itself when it decides it needs to.
        recall_block = ""
        if _looks_like_past_reference(prompt):
            # Tighter timeout on voice — recall taking 2s+ would defeat
            # the snappy voice latency target.
            timeout = 1.2 if mode == "voice" else 2.5
            recall_block = await self._fetch_recall_block(prompt,
                                                          timeout=timeout)

        system_prompt, messages, shard_sel = self._assembler.assemble(
            user_message=effective_prompt,
            state=self._state,
            history=self._history.to_ollama_messages(),
            mode=mode,
            forced_shard=self._state.forced_shard,
            recall_block=recall_block,
        )

        # Add to history now that the assembler has captured the prior state.
        self._history.add_user(prompt)
        logger.debug(
            "ask: shard=%s load=%.2f mode=%s",
            shard_sel.shard.value, self._state.cognitive_load, mode,
        )

        # Route.
        use_claude = (
            mode != "voice"
            and self._claude.is_configured()
            and await self._should_use_claude(prompt)
        )

        if stream and not use_claude:
            return self._stream_ollama(
                system_prompt, messages, shard_sel.shard.value, mode=mode
            )
        elif use_claude:
            response = await self._ask_claude(
                system_prompt, messages, shard_sel.shard.value, mode=mode
            )
        else:
            response = await self._ask_ollama(
                system_prompt, messages, shard_sel.shard.value, mode=mode
            )

        return response

    async def set_shard(self, shard_name: str) -> bool:
        """Force a specific shard. Returns True if shard name is valid."""
        try:
            Shard(shard_name.lower())
            self._state.forced_shard = shard_name.lower()
            logger.info("shard forced to: %s", shard_name)
            return True
        except ValueError:
            return False

    def clear_history(self) -> None:
        """Reset conversation. Called at session boundary or on user command."""
        self._history.clear()
        logger.info("conversation history cleared")

    def get_state_summary(self) -> dict:
        """Return current state summary for logging/debugging."""
        return {
            "cognitive_load": self._state.cognitive_load,
            "tension": self._state.tension,
            "tension_band": self._state.tension_band,
            "focus_category": self._state.focus_category,
            "focus_app": self._state.focus_app,
            "snapshot_age_secs": round(self._state.snapshot_age_secs, 1),
            "patterns": len(self._state.patterns),
            "history_turns": len(self._history) // 2,
        }

    # ── Internal routing ─────────────────────────────────────────────────

    async def _ask_ollama(
        self, system_prompt: str, messages: list[dict], shard: str, mode: str = "default"
    ) -> str:
        start = time.monotonic()
        # Use the bigger local model when the user is calm; we have CPU/GPU
        # headroom for it and the larger model gives better long-form answers.
        model = (
            self._cfg.ollama_model_large
            if self._state.cognitive_load < 0.5
            else self._cfg.ollama_model
        )
        try:
            response = await self._ollama.chat(
                system_prompt=system_prompt,
                messages=messages,
                model=model,
                temperature=self._response_temperature(),
                max_tokens=self._max_tokens(mode),
            )
        except Exception as exc:
            logger.warning("Ollama failed: %s — trying Claude fallback", exc)
            if self._claude.is_configured():
                response = await self._ask_claude(system_prompt, messages, shard, mode=mode)
                # _ask_claude already post-processed + added to history;
                # return early to avoid double-handling.
                return response
            else:
                response = "I'm having trouble reaching the local model. Is Ollama running?"

        elapsed = time.monotonic() - start
        response = self._post_process(response, shard, elapsed)
        self._history.add_assistant(response)
        await self._publish_response(response, shard)
        return response

    async def _ask_claude(
        self, system_prompt: str, messages: list[dict], shard: str, mode: str = "default"
    ) -> str:
        start = time.monotonic()
        try:
            response = await self._claude.chat(
                system_prompt=system_prompt,
                messages=messages,
                temperature=self._response_temperature(),
            )
        except Exception as exc:
            logger.error("Claude API failed: %s", exc)
            # Fall back to Ollama (non-streaming, default model).
            response = await self._ollama.chat(system_prompt, messages)

        elapsed = time.monotonic() - start
        response = self._post_process(response, shard, elapsed)
        self._history.add_assistant(response)
        await self._publish_response(response, shard)
        return response

    def _stream_ollama(
        self, system_prompt: str, messages: list[dict], shard: str, mode: str = "default"
    ) -> AsyncIterator[str]:
        """Streaming wrapper — collects full response for history after streaming."""
        collected: list[str] = []
        service = self

        async def _gen() -> AsyncIterator[str]:
            try:
                async for chunk in service._ollama.chat_stream(
                    system_prompt, messages
                ):
                    collected.append(chunk)
                    yield chunk
            finally:
                full = "".join(collected)
                full = service._post_process(full, shard, 0)
                service._history.add_assistant(full)
                # Fire-and-forget the publish; we're already returning.
                asyncio.ensure_future(service._publish_response(full, shard))

        return _gen()

    async def _should_use_claude(self, prompt: str) -> bool:
        """
        Heuristic complexity classifier. Claude is used for:
        - Long analytical prompts (> 200 chars with multiple clauses)
        - High correction-rate context (preference engine signal)
        - Explicit "explain in depth" type requests
        """
        if len(prompt) > 200 and any(
            w in prompt.lower()
            for w in (
                "explain", "compare", "analyse", "analyze",
                "design", "architecture", "why does",
            )
        ):
            return True

        load_band = _load_band(self._state.cognitive_load)
        sel = select_shard(
            self._state.focus_category,
            self._state.cognitive_load,
            self._state.tension_band,
        )
        rate = self._preference.correction_rate(sel.shard.value, load_band)
        if rate > 0.4:  # 40%+ correction rate → try Claude
            logger.info(
                "high correction rate %.2f for shard %s → routing to Claude",
                rate, sel.shard.value,
            )
            return True

        return False

    def _response_temperature(self) -> float:
        """Lower temperature when user is highly loaded (more precise = better)."""
        if self._state.cognitive_load > 0.75:
            return 0.3
        return 0.7

    def _max_tokens(self, mode: str = "default") -> int:
        """Token budget for the current request.

        Voice mode gets a healthy budget too — TTS handles the length, and the
        old 512-token cap was clipping replies mid-sentence. The voice
        addendum already nudges the model to keep things short.
        """
        if mode == "voice":
            # ~150 words = comfortable 30s of speech. Generous enough that
            # the model doesn't truncate; the prompt addendum keeps it tight.
            return 1024
        if self._state.cognitive_load > self._cfg.high_load_threshold:
            return 1536
        return 4096

    def _post_process(self, response: str, shard: str, elapsed: float) -> str:
        """Strip tool call blocks, sanitize persona violations, log."""
        tool_calls = parse_tool_calls(response)
        if tool_calls:
            logger.info("tool calls found: %s", [t.name for t in tool_calls])
            asyncio.ensure_future(self._publish_tool_calls(tool_calls))
        clean = strip_tool_calls(response)
        clean = _sanitize_persona(clean, self._user_name())
        self._preference.on_response(shard, self._state.cognitive_load, clean)
        if elapsed > 0:
            logger.debug(
                "response in %.2fs shard=%s tokens~%d",
                elapsed, shard, len(clean.split()),
            )
        return clean

    def _user_name(self) -> str:
        return (self._cfg.user_name or "").strip()

    async def _visual_answer(self, prompt: str, mode: str) -> Optional[str]:
        """Capture screen + ask LLaVA. Returns None on any failure so the
        caller falls back to the normal text path.
        """
        import asyncio  # local to keep cold-path imports tidy
        try:
            image_b64 = await asyncio.to_thread(capture_screen_b64)
        except Exception as exc:  # noqa: BLE001
            logger.warning("screen capture failed: %s", exc)
            return None
        if not image_b64:
            return None
        # Prefer llama3.2-vision (much better on UI text + specifics)
        # over llava:latest. Fall back to llava if 11b isn't pulled yet.
        for model in ("llama3.2-vision:11b", "llava:latest"):
            try:
                response = await self._ollama.chat_with_images(
                    system_prompt=VISION_SYSTEM_PROMPT,
                    user_text=prompt,
                    images_b64=[image_b64],
                    model=model,
                    temperature=0.3,
                    max_tokens=self._max_tokens(mode),
                )
                if response and response.strip():
                    break
            except Exception as exc:  # noqa: BLE001
                logger.warning("vision call (%s) failed: %s", model, exc)
                response = None
        else:
            return None
        if response is None:
            return None
        response = (response or "").strip()
        if not response:
            return None
        logger.info("visual_answer: %d chars", len(response))
        return response

    async def _publish_response(self, response: str, shard: str) -> None:
        if self._bridge:
            await self._bridge.publish("llm_response", {
                "text": response,
                "shard": shard,
                "cognitive_load": self._state.cognitive_load,
                "response_len": len(response),
                "ts_unix_ms": int(time.time() * 1000),
            })

    async def _publish_tool_calls(self, calls: list[ToolCall]) -> None:
        """Forward parsed tool calls to Module E (tool-service).

        Module E subscribes to ``tool_call_request`` and expects a flat
        ``{name, args, request_id}`` payload — one event per call. The
        older bulk ``tool_call_requested`` format (calls=[…]) was never
        picked up, which is why "open Spotify" silently no-op'd.
        """
        if not self._bridge:
            return
        for c in calls:
            request_id = f"llm-{int(time.time()*1000)}-{c.name}"
            try:
                await self._bridge.publish("tool_call_request", {
                    "request_id": request_id,
                    "name": c.name,
                    "args": c.args,
                })
            except Exception:  # noqa: BLE001
                logger.exception("tool_call_request publish failed for %s", c.name)

    # ── WS event handling ─────────────────────────────────────────────────

    async def _handle_event(self, event: dict) -> None:
        kind = event.get("kind", "")
        payload = event.get("payload", {})
        if kind == "voice_transcript":
            await self._handle_voice_transcript(payload)
        elif kind == "insight_snapshot":
            self._state.update_snapshot(payload)
        elif kind == "productivity_prior_update":
            self._state.update_priors(payload)
        elif kind == "patterns_update":
            self._state.update_patterns(payload)
        elif kind == "spotify_now_playing":
            self._state.update_spotify(payload)
        elif kind == "browser_tab":
            self._state.update_browser_tab(payload)
        elif kind == "gh_activity":
            self._state.update_gh_activity(payload)
        elif kind == "calendar_upcoming":
            self._state.update_calendar(payload)
        elif kind == "gmail_unread":
            self._state.update_gmail(payload)
        elif kind == "app_detail":
            self._state.update_app_detail(payload)
        elif kind == "git_activity":
            self._state.update_git_activity(payload)
        elif kind == "code_change":
            self._state.update_code_change(payload)
        elif kind == "boot_reflection":
            self._state.update_boot_reflection(payload)
        elif kind == "claude_session_update":
            self._state.update_claude_session(payload)
        elif kind == "weather_update":
            self._state.update_weather(payload)
        elif kind == "stocks_update":
            self._state.update_stocks(payload)
        elif kind == "news_update":
            self._state.update_news(payload)
        elif kind == "system_info":
            self._state.update_sysinfo(payload)
        elif kind == "system_health_update":
            self._state.update_syshealth(payload)
        elif kind == "emotion_state_changed":
            self._state.update_emotion(payload)
        elif kind == "recall_query_result":
            self._on_recall_result(payload)

    def _on_recall_result(self, payload: dict) -> None:
        """Wake any pending recall waiter (FIFO).

        The service handles one prompt at a time, so a single global
        FIFO queue is enough — concurrent recall requests don't happen
        in practice.
        """
        while self._recall_waiters:
            fut = self._recall_waiters.pop(0)
            if not fut.done():
                fut.set_result(payload)
                return

    async def _fetch_recall_block(self, query: str,
                                  timeout: float = 2.5) -> str:
        """Publish a recall_query_request and await the next result.

        Returns the formatted prompt_block string (empty on timeout,
        missing bridge, or empty result). Single-prompt-at-a-time, so a
        FIFO waiter queue is enough — no per-request correlation id.
        """
        if self._bridge is None:
            return ""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._recall_waiters.append(fut)
        try:
            await self._bridge.publish("recall_query_request", {
                "kind": "search",
                "query": query,
                "top_k": 5,
                "include_reflections": True,
                "include_facts": True,
            })
            payload = await asyncio.wait_for(fut, timeout=timeout)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            return ""
        finally:
            try:
                self._recall_waiters.remove(fut)
            except ValueError:
                pass
        block = str(payload.get("prompt_block") or "").strip()
        if block:
            logger.info("recall: injected %d chars for prompt %r",
                        len(block), query[:60])
        return block

    async def _handle_voice_transcript(self, payload: dict) -> None:
        text = (payload.get("text") or "").strip()
        if not text:
            return
        logger.info("voice_transcript received: %r", text[:80])
        try:
            response = await self.ask(text, mode="voice")
        except Exception as exc:
            logger.error("ask() failed for voice_transcript: %s", exc)
            if self._bridge:
                await self._bridge.publish("llm_response", {
                    "text": "Sorry, I ran into an error processing that.",
                    "error": True,
                    "ts_unix_ms": int(time.time() * 1000),
                })

    # ── Run loop ──────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._bridge = UltronBridge(
            url=self._cfg.ws_url,
            token=self._cfg.token,
            on_event=self._handle_event,
            subscribe_to=[
                "voice_transcript",
                "insight_snapshot",
                "productivity_prior_update",
                "patterns_update",
                # Long-term memory.
                "recall_query_result",
                # Integrations sidecar — see ultron_bridges/.
                "spotify_now_playing",
                "browser_tab",
                "gh_activity",
                "calendar_upcoming",
                "gmail_unread",
                "app_detail",
                "git_activity",
                "code_change",
                "boot_reflection",
                "claude_session_update",
                # Daily-data + system-info bridges (spectacle HUD work).
                "weather_update",
                "stocks_update",
                "news_update",
                "system_info",
                # System health monitor.
                "system_health_update",
                # Emotional intelligence layer.
                "emotion_state_changed",
            ],
            role="llm-client",
        )
        logger.info(
            "LLMService starting — model=%s claude=%s",
            self._cfg.ollama_model,
            "configured" if self._claude.is_configured() else "not configured",
        )
        # Ask the data bridges to republish so our state catches up
        # immediately on a cold start. Otherwise the first "what's the
        # weather" misses the cache and falls through to the LLM.
        async def _kick_refresh() -> None:
            try:
                await asyncio.sleep(0.5)  # let the bridge subscribe-ack settle
                for k in ("weather_request", "stocks_request",
                          "news_request", "system_info_request"):
                    try:
                        await self._bridge.publish(k, {})
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001
                logger.debug("startup refresh kick failed (non-fatal)")

        asyncio.create_task(_kick_refresh())
        await self._bridge.run_forever()
