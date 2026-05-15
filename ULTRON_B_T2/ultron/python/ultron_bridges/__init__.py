"""ultron_bridges — external-service integration sidecar.

Each `Bridge` subclass polls one external service (Spotify, GitHub, etc.)
and publishes typed events onto the WS bus. The `BridgesService` owns
the shared `UltronBridge` WS client and supervises all enabled bridges
so one failing bridge can't take the others down.
"""
from .base import Bridge, BridgePublishFn
from .config import BridgesConfig, load_bridges_config
from .service import BridgesService

__all__ = [
    "Bridge",
    "BridgePublishFn",
    "BridgesConfig",
    "BridgesService",
    "load_bridges_config",
]
