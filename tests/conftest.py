"""
The shared fixtures of the test suite.

`mass` gives a started server in dev mode with this repository as its local provider folder
(the server's own testing plugin); `mock_client` is the factory of the in-memory Wyoming TTS
servers the tests render against, in place of real ones.
"""

from __future__ import annotations

import pytest
from wyoming.client import AsyncClient

from tests.mock_client import MockClientFactory

pytest_plugins = ["music_assistant.testing"]


@pytest.fixture(autouse=True)
def mock_client(monkeypatch: pytest.MonkeyPatch) -> MockClientFactory:
    """
    Route provider clients to the in-memory Wyoming TTS servers the test created.

    Every client a provider opens is served by one of those servers; an address without a
    server is refused, like one where nothing listens.
    """
    factory = MockClientFactory()
    monkeypatch.setattr(AsyncClient, "from_uri", factory.from_uri)
    return factory
