"""
Wyoming Text-to-speech provider for Music Assistant.

Exposes a Wyoming protocol TTS server (such as wyoming-piper) as one TTS engine per
discovered voice, for announcements and AI Radio. Rendered clips are cached on disk
and served from a route on the streamserver.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .provider import SUPPORTED_FEATURES, WyomingTTSProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return WyomingTTSProvider(mass, manifest, config, SUPPORTED_FEATURES)
