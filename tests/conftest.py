"""Shared fixtures.

pytest-homeassistant-custom-component supplies the ``hass`` fixture. We
enable custom-integration discovery so the ``finder_you`` package under
``custom_components/`` is importable from inside HA test runs without
needing to be installed.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow our custom_components/finder_you to be loaded under hass."""
    yield
