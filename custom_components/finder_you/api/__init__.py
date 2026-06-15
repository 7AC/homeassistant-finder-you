"""Finder YOU Python API client.

A reverse-engineered async client for the ``you-api.iot.findernet.com``
gRPC service. Mints tokens via the Android-style OAuth flow and maintains
a persistent HTTP/2 connection with Android-exact framing so the gateway
accepts our control commands.
"""
from .client import FinderApiError, FinderHomeClient, GatewayOfflineError
from .oauth import OAuthError, fetch_token, refresh_token
from .plant import (
    Shutter,
    extract_shutter_positions,
    extract_shutter_states,
    parse_plant,
)

__all__ = [
    "FinderApiError",
    "FinderHomeClient",
    "GatewayOfflineError",
    "OAuthError",
    "Shutter",
    "extract_shutter_positions",
    "extract_shutter_states",
    "fetch_token",
    "parse_plant",
    "refresh_token",
]
