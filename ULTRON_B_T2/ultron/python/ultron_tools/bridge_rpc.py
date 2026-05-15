"""Process-global helper that lets tool handlers do a WS round-trip.

Each ULTRON service runs in its own process. The tools that surface
read-only views over other services (``money_query``, ``code_query``,
…) therefore cannot reach them via in-process singletons. They must
publish a ``*_query_request`` and wait for the matching
``*_query_result`` on the bus.

``ToolService`` calls ``set_bridge(bridge, kinds)`` at startup to
install the live bridge here, and forwards every event in ``kinds`` to
``deliver_result()``. Handlers call ``request_response(...)``.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger("ultron.tools.rpc")

# These are the (request, response) topic pairs we proxy.
RESULT_KINDS: tuple[str, ...] = (
    "dopamine_query_result",
    "wellness_query_result",
    "money_query_result",
    "plan_query_result",
    "code_query_result",
    "kg_query_result",
)


_bridge: Any = None  # an UltronBridge — typed Any to avoid import cycle
_waiters: dict[str, list[asyncio.Future[dict[str, Any]]]] = {
    k: [] for k in RESULT_KINDS
}


def set_bridge(bridge: Any) -> None:
    """Called by ``ToolService.run`` once the bridge is built."""
    global _bridge
    _bridge = bridge


def deliver_result(kind: str, payload: dict[str, Any]) -> None:
    """Called by ``ToolService._handle_event`` for any result topic."""
    if kind not in _waiters:
        return
    queue = _waiters[kind]
    while queue:
        fut = queue.pop(0)
        if not fut.done():
            fut.set_result(payload)
            return


async def request_response(
    request_kind: str,
    payload: dict[str, Any],
    response_kind: str,
    timeout: float = 5.0,
) -> Optional[dict[str, Any]]:
    """Publish ``request_kind`` and return the next ``response_kind`` payload.

    Returns ``None`` on timeout, missing bridge, or unknown response_kind.
    Concurrent callers for the same ``response_kind`` queue FIFO — the
    response order matches the request order, which holds as long as
    no third party is also publishing on those topics.
    """
    if _bridge is None:
        return None
    if response_kind not in _waiters:
        logger.warning("response_kind %r not registered for rpc", response_kind)
        return None
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[dict[str, Any]] = loop.create_future()
    _waiters[response_kind].append(fut)
    try:
        await _bridge.publish(request_kind, payload)
        return await asyncio.wait_for(fut, timeout=timeout)
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
        return None
    finally:
        try:
            _waiters[response_kind].remove(fut)
        except ValueError:
            pass
