"""
The Music Assistant provider of the Wyoming TTS plugin.

One TTS engine per voice that passes the provider's filters, for announcements
and AI Radio. Rendered clips are cached on disk and served from a route on the
streamserver. The protocol layer is in wyoming.py.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import struct
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import aiofiles
from aiofiles.os import makedirs, remove, replace
from aiofiles.os import path as aiopath
from aiohttp import web
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.helpers.ffmpeg import get_ffmpeg_stream
from music_assistant.models.plugin import PluginProvider, TTSEngine

from .const import (
    CONF_AVAILABLE_VOICES,
    CONF_OUTPUT_FORMAT,
    CONF_SERVER_URI,
    CONF_SYNTHESIS_TIMEOUT,
    CONF_VOICE_LANGUAGES,
    CONF_VOICES,
    DOMAIN,
)
from .wyoming import (
    DiscoveredVoice,
    discover_voices,
    normalize_server_uri,
    synthesize,
    voice_language_matches,
    voice_matches,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant

# the output formats a rendered clip can be stored in, mapped to their content type
OUTPUT_FORMATS: dict[str, ContentType] = {
    "mp3": ContentType.MP3,
    "wav": ContentType.WAV,
}
DEFAULT_OUTPUT_FORMAT = "mp3"
# the maximum time in seconds a render may take, before the call is failed
DEFAULT_SYNTHESIS_TIMEOUT = 30
MIN_SYNTHESIS_TIMEOUT = 5
MAX_SYNTHESIS_TIMEOUT = 30
# rendered clips older than this are removed from the on-disk cache
CACHE_MAX_AGE = 24 * 3600
# speech needs no high bitrate: 96k keeps the cached clips small
MP3_BITRATE = "96k"

# clip file names are sha256 hexdigests; anything else in the cache directory is not ours
FILE_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")

SUPPORTED_FEATURES = {ProviderFeature.TTS}


def to_wav(pcm: bytes, rate: int) -> bytes:
    """
    Wrap 16-bit little-endian mono PCM in a RIFF/WAVE container.

    :param pcm: The raw PCM audio.
    :param rate: The sample rate of the audio in Hz.
    """
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(pcm),
        b"WAVE",
        b"fmt ",
        16,  # fmt chunk size
        1,  # audio format: PCM
        1,  # channels
        rate,  # sample rate
        rate * 2,  # byte rate
        2,  # block align
        16,  # bits per sample
        b"data",
        len(pcm),
    )
    return header + pcm


async def to_mp3(pcm: bytes, rate: int) -> bytes:
    """
    Encode 16-bit little-endian mono PCM to MP3 with ffmpeg.

    :param pcm: The raw PCM audio.
    :param rate: The sample rate of the audio in Hz.
    """

    async def _feed() -> AsyncGenerator[bytes]:
        yield pcm

    input_format = AudioFormat(
        content_type=ContentType.PCM_S16LE, sample_rate=rate, bit_depth=16, channels=1
    )
    output_format = AudioFormat(
        content_type=ContentType.MP3, sample_rate=rate, bit_depth=16, channels=1
    )
    data = bytearray()
    async for chunk in get_ffmpeg_stream(
        _feed(), input_format, output_format, extra_output_args=["-b:a", MP3_BITRATE]
    ):
        data += chunk
    return bytes(data)


class WyomingTTSProvider(PluginProvider):
    """Text-to-speech provider backed by a Wyoming protocol TTS server."""

    _cache_dir: str
    _server_uri: str
    _render_lock: asyncio.Lock
    # what the server advertised at load; the selected voices are the filtered view the
    # filters currently pass, and the engines are built from them. The lists are created
    # here, not in the async init: the config entries (which show the available voices)
    # are read while the provider loads, before that init has run
    _discovered: list[DiscoveredVoice]
    _selected: list[DiscoveredVoice]
    _engines: list[TTSEngine]

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature] | None,
    ) -> None:
        """Initialize the provider."""
        super().__init__(mass, manifest, config, supported_features)
        self._discovered = []
        self._selected = []
        self._engines = []

    # rendered clips by file id; the route only serves paths from this index
    _clips: dict[str, str]
    _unregister_route: Callable[[], None] | None = None

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._cache_dir = str(Path(self.mass.cache_path) / self.domain / self.instance_id)
        self._server_uri = normalize_server_uri(str(self.get_setup_value(CONF_SERVER_URI)))
        self._render_lock = asyncio.Lock()
        # clips rendered before a restart stay playable
        self._clips = await self._index_cache()
        self._discovered = []
        self._selected = []
        self._engines = []
        try:
            self._discovered = await discover_voices(self._server_uri)
        except MusicAssistantError:
            # the setup flow already verified the server, so this is unexpected:
            # it may be down right now, and the reload below tries again
            self.logger.exception("Could not discover the TTS voices of the Wyoming server")
            self.available = False
            # armed under the load path's task id so any (re)load starting before it
            # fires cancels it
            self.mass.call_later(
                5,
                self.mass.load_provider,
                self.instance_id,
                allow_retry=True,
                task_id=f"load_provider_{self.instance_id}",
            )
        self._rebuild_engines()
        self._unregister_route = self.mass.streams.register_dynamic_route(
            self._route_path, self._handle_speech_request
        )

    async def unload(self, is_removed: bool = False) -> None:  # noqa: FBT001, FBT002 - base signature
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        is_removed will be set to True when the provider is removed from the configuration.
        """
        await super().unload(is_removed)
        if unregister := self._unregister_route:
            self._unregister_route = None
            unregister()
        if is_removed:
            await asyncio.to_thread(shutil.rmtree, self._cache_dir, ignore_errors=True)

    async def update_config(self, config: ProviderConfig, changed_keys: set[str]) -> None:
        """
        Handle logic when the config is updated.

        All the options of the provider are read dynamically at render time, so a change
        of only those never needs a reload: the voice and language filters are re-applied
        to the voices the server advertised, the rest is read from the config on every
        render. A filter that matches no voice is a legal state: it exposes no engines
        until it is fixed, and the available-voices entry shows that immediately.
        """
        filter_keys = {f"values/{CONF_VOICES}", f"values/{CONF_VOICE_LANGUAGES}"}
        own_value_keys = {
            f"values/{CONF_OUTPUT_FORMAT}",
            f"values/{CONF_SYNTHESIS_TIMEOUT}",
            *filter_keys,
        }
        if changed_keys <= own_value_keys:
            self.config = config
            if changed_keys & filter_keys:
                self._rebuild_engines()
            return
        await super().update_config(config, changed_keys)

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """
        Return the (options) config entries for this provider instance.

        The server address is collected by the setup flow (see setup_flow.py); the
        options here filter the exposed voices and tune the rendering.
        """
        return (
            ConfigEntry(
                key=CONF_AVAILABLE_VOICES,
                type=ConfigEntryType.LABEL,
                # the list is resolved into the entry's label by the server, so it
                # tracks the filters: changing them re-resolves the entries on read
                translation_params=[self._available_voices_text()],
                category="generic",
            ),
            ConfigEntry(
                key=CONF_VOICES,
                type=ConfigEntryType.STRING,
                # no options: the filter takes glob patterns, and a populated options
                # list would render as a strict select
                multi_value=True,
                required=False,
                default_value=[],
                category="features",
                requires_reload=False,
            ),
            ConfigEntry(
                key=CONF_VOICE_LANGUAGES,
                type=ConfigEntryType.STRING,
                multi_value=True,
                required=False,
                default_value=[],
                # the languages the server advertises, so the client renders a select;
                # a hand-edited value may still be a glob pattern
                options=[ConfigValueOption(code, code) for code in self._known_language_codes()],
                category="features",
                requires_reload=False,
            ),
            ConfigEntry(
                key=CONF_OUTPUT_FORMAT,
                # a string with options renders as a select
                type=ConfigEntryType.STRING,
                required=False,
                default_value=DEFAULT_OUTPUT_FORMAT,
                options=[ConfigValueOption(fmt) for fmt in OUTPUT_FORMATS],
                category="features",
                requires_reload=False,
            ),
            ConfigEntry(
                key=CONF_SYNTHESIS_TIMEOUT,
                type=ConfigEntryType.INTEGER,
                required=False,
                default_value=DEFAULT_SYNTHESIS_TIMEOUT,
                range=(MIN_SYNTHESIS_TIMEOUT, MAX_SYNTHESIS_TIMEOUT),
                category="features",
                requires_reload=False,
            ),
        )

    async def get_tts_engines(self) -> list[TTSEngine]:
        """Return one TTS engine per voice that matches the voice filter."""
        return self._engines

    async def get_tts_message(
        self,
        message: str,
        language: str | None = None,
        engine_id: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> StreamDetails:
        """
        Render the given message as speech.

        :param message: The text to convert to speech.
        :param language: Ignored: each engine is one fixed voice of the server, so
            there is no language to choose within an engine.
        :param engine_id: The ``program:voice`` to render with; defaults to the first
            available voice.
        :param options: Ignored: this provider has no configurable TTS options.
        :return: StreamDetails for the (cached) audio clip.
        """
        if not self._engines:
            msg = "The Wyoming server exposes no voice matching the configured filters"
            raise MusicAssistantError(
                msg,
                translation_key="no_voices",
                translation_owner=f"provider.{DOMAIN}",
            )
        if engine_id is None:
            engine_id = self._engines[0].id
        program, _, voice = engine_id.partition(":")
        if not voice or not any(
            discovered.program == program and discovered.voice == voice
            for discovered in self._discovered
        ):
            msg = f"Unknown TTS engine {engine_id!r} for this provider"
            raise MusicAssistantError(
                msg,
                translation_key="voice_not_found",
                translation_owner=f"provider.{DOMAIN}",
            )
        file_id, output_format = await self._render_speech(program, voice, message)
        audio_format = AudioFormat(content_type=OUTPUT_FORMATS[output_format])
        return StreamDetails(
            provider=self.instance_id,
            item_id=file_id,
            audio_format=audio_format,
            media_type=MediaType.SOUND_EFFECT,
            stream_type=StreamType.HTTP,
            path=f"{self.mass.streams.base_url}{self._route_path}?id={file_id}",
        )

    @property
    def _route_path(self) -> str:
        """Return the path of this instance's dynamic route on the streamserver."""
        return f"/{self.instance_id}_speech"

    @property
    def instance_name_postfix(self) -> str | None:
        """Distinguish instances by the Wyoming server they point at, not by number."""
        value = self.get_setup_value(CONF_SERVER_URI)
        if not value:
            return None
        parts = urlsplit(normalize_server_uri(str(value)))
        if parts.hostname:
            return f"{parts.hostname}:{parts.port}" if parts.port else parts.hostname
        return None

    @property
    def _output_format(self) -> str:
        """Return the configured output format, the default when it is unset or unknown."""
        value = str(self.get_config_value(CONF_OUTPUT_FORMAT, DEFAULT_OUTPUT_FORMAT))
        return value if value in OUTPUT_FORMATS else DEFAULT_OUTPUT_FORMAT

    @property
    def _synthesis_timeout(self) -> float:
        """Return the configured overall synthesis deadline in seconds."""
        value = self.get_config_value(
            CONF_SYNTHESIS_TIMEOUT, DEFAULT_SYNTHESIS_TIMEOUT, return_type=float
        )
        return float(value)

    def _known_language_codes(self) -> list[str]:
        """Return the language codes the discovered voices speak, de-duplicated and sorted."""
        return sorted({language for voice in self._discovered for language in voice.languages})

    def _filter_values(self, config: ProviderConfig, key: str) -> list[str]:
        """Return the filter values for the given config key, stripped and de-duplicated."""
        value = config.get_value(key)
        values = value if isinstance(value, list) else [value]
        return list(
            dict.fromkeys(
                matcher.strip()
                for matcher in values
                if isinstance(matcher, str) and matcher.strip()
            )
        )

    def _selected_voices(
        self,
        name_matchers: list[str],
        language_matchers: list[str],
    ) -> list[DiscoveredVoice]:
        """Return the discovered voices that pass the given name and language matchers."""
        return [
            voice
            for voice in self._discovered
            if (
                not name_matchers
                or any(
                    voice_matches(matcher, voice.program, voice.voice) for matcher in name_matchers
                )
            )
            and (
                not language_matchers
                or any(
                    voice_language_matches(matcher, voice.languages)
                    for matcher in language_matchers
                )
            )
        ]

    def _rebuild_engines(self) -> None:
        """(Re)build the engine list from the discovered voices and the filters."""
        name_matchers = self._filter_values(self.config, CONF_VOICES)
        language_matchers = self._filter_values(self.config, CONF_VOICE_LANGUAGES)
        selected = self._selected_voices(name_matchers, language_matchers)
        if not selected:
            self.logger.warning(
                "No voice of the Wyoming server matches the configured filters "
                "(voices: %s, languages: %s)",
                name_matchers,
                language_matchers,
            )
        self._selected = selected
        # the program always prefixes the name: with several instances, the voice name
        # alone does not say where the speech is rendered
        self._engines = [
            TTSEngine(
                id=f"{voice.program}:{voice.voice}",
                name=f"{voice.program} / {voice.voice}",
                provider=self,
            )
            for voice in selected
        ]

    def _available_voices_text(self) -> str:
        """Format the voices the filters currently expose, grouped by program."""
        if not self._selected:
            return "none available"
        by_program: dict[str, list[str]] = {}
        for voice in self._selected:
            languages = f" ({', '.join(voice.languages)})" if voice.languages else ""
            by_program.setdefault(voice.program, []).append(f"{voice.voice}{languages}")
        return "; ".join(
            f"{program}: {', '.join(voices)}" for program, voices in by_program.items()
        )

    async def _handle_speech_request(self, request: web.Request) -> web.FileResponse | web.Response:
        """Serve a rendered speech clip by its file id."""
        if not (file_id := request.query.get("id")):
            return web.Response(status=400, text="Missing id")
        # the id from the url is only a lookup key, never a path component
        if not (file_path := self._clips.get(file_id)):
            raise web.HTTPNotFound
        if not await aiopath.isfile(file_path):
            self._clips.pop(file_id, None)
            raise web.HTTPNotFound
        return web.FileResponse(file_path)

    async def _render_speech(self, program: str, voice: str, message: str) -> tuple[str, str]:
        """
        Render the message to a file in the cache directory.

        :param program: The TTS program to render with.
        :param voice: The voice to render with.
        :param message: The text to convert to speech.
        :return: The file id of the rendered clip and the format it is stored in.
        """
        output_format = self._output_format
        # the server and the program are part of the identity: repointing an instance or
        # switching programs must not reuse clips
        digest = f"{self._server_uri}\0{program}\0{voice}\0{output_format}\0{message}"
        file_id = hashlib.sha256(digest.encode()).hexdigest()
        file_path = str(Path(self._cache_dir) / f"{file_id}.{output_format}")
        async with self._render_lock:
            if await aiopath.isfile(file_path):
                # a reused clip is played right away, so keep it out of reach of the reaper
                with suppress(OSError):
                    await asyncio.to_thread(os.utime, file_path, None)
                self._clips[file_id] = file_path
                return file_id, output_format
            await makedirs(self._cache_dir, exist_ok=True)
            try:
                pcm, rate = await synthesize(
                    self._server_uri, program, voice, message, self._synthesis_timeout
                )
                if output_format == "mp3":
                    audio_data = await to_mp3(pcm, rate)
                else:
                    audio_data = to_wav(pcm, rate)
            except TimeoutError as err:
                raise MusicAssistantError(
                    f"Synthesis timed out after {self._synthesis_timeout:.0f} seconds",
                    translation_key="synthesis_timeout",
                    translation_args=[f"{self._synthesis_timeout:.0f}"],
                    translation_owner=f"provider.{DOMAIN}",
                ) from err
            # write to a temp file first so an interrupted render leaves no partial clip
            tmp_path = f"{file_path}.tmp"
            try:
                async with aiofiles.open(tmp_path, "wb") as _file:
                    await _file.write(audio_data)
                await replace(tmp_path, file_path)
            except OSError:
                with suppress(OSError):
                    await remove(tmp_path)
                raise
            self._clips[file_id] = file_path
        await self._reap_cache()
        return file_id, output_format

    async def _reap_cache(self) -> None:
        """Remove rendered clips that are older than the maximum cache age."""

        def _reap() -> list[str]:
            reaped: list[str] = []
            cutoff = time.time() - CACHE_MAX_AGE
            with os.scandir(self._cache_dir) as entries:
                for entry in entries:
                    with suppress(OSError):
                        if (
                            entry.is_file(follow_symlinks=False)
                            and entry.stat(follow_symlinks=False).st_mtime < cutoff
                        ):
                            Path(entry.path).unlink()
                            reaped.append(Path(entry.name).stem)
            return reaped

        # reaping is opportunistic housekeeping and must never break playback
        with suppress(OSError):
            reaped = await asyncio.to_thread(_reap)
            for file_id in reaped:
                self._clips.pop(file_id, None)

    async def _index_cache(self) -> dict[str, str]:
        """Return the clips present in the cache directory, keyed by file id."""

        def _index() -> dict[str, str]:
            # only adopt what this provider could have written itself: anything else that
            # ended up in the directory must never become reachable through the route
            paths: dict[str, str] = {}
            with os.scandir(self._cache_dir) as entries:
                for entry in entries:
                    if (
                        entry.is_file(follow_symlinks=False)
                        and Path(entry.name).suffix.removeprefix(".") in OUTPUT_FORMATS
                        and FILE_ID_PATTERN.match(Path(entry.name).stem)
                    ):
                        paths[Path(entry.name).stem] = entry.path
            return paths

        try:
            return await asyncio.to_thread(_index)
        except OSError:
            # nothing rendered yet: the cache directory is created on the first render
            return {}
