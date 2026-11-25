"""
An in-memory substitute for a Wyoming TTS server, for the tests.

It stands in for the network at the client seam: the tests patch
``wyoming.client.AsyncClient.from_uri`` with a ``MockClientFactory``, so a test drives the
provider's whole stack, from the setup flow to the served clip, without a socket, a server
process or the wire protocol. The synthesized audio is a deterministic one second 22 050 Hz
sine wave, so renders are byte-stable.
"""

from __future__ import annotations

import asyncio
import math
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from wyoming.audio import AudioChunk, AudioStop
from wyoming.error import Error
from wyoming.info import Attribution, Describe, Info, SelectProgram, TtsProgram, TtsVoice
from wyoming.tts import Synthesize

if TYPE_CHECKING:
    from wyoming.event import Event

RATE = 22050
WIDTH = 2
CHANNELS = 1
SAMPLES_PER_CHUNK = RATE // 10  # 0.1 s per chunk


def _render_sine() -> bytes:
    """Return one second of a 440 Hz sine wave as s16le mono."""
    frames = bytearray()
    for i in range(RATE):
        sample = int(12000 * math.sin(2 * math.pi * 440 * i / RATE))
        frames += struct.pack("<h", sample)
    return bytes(frames)


# the same audio for every request, so a render is byte-stable
AUDIO = _render_sine()


@dataclass(frozen=True, slots=True)
class Voice:
    """A voice of a program."""

    name: str
    languages: tuple[str, ...] = ("en",)
    installed: bool = True
    description: str | None = None


@dataclass(frozen=True, slots=True)
class Program:
    """A TTS program the server advertises."""

    name: str
    voices: tuple[Voice, ...]
    installed: bool = True


class MockServer:
    """The state of a mocked Wyoming TTS server: what it advertises and how it renders."""

    def __init__(self, programs: list[Program], uri: str = "tcp://127.0.0.1:10200") -> None:
        """
        Initialize the server.

        :param programs: The TTS programs, in the order advertised.
        :param uri: The address clients connect to it at.
        """
        self.programs = programs
        self.uri = uri
        # "ok" answers with audio; "error" answers synthesize with an error event;
        # "no_audio" stops without sending any audio; "silent" never answers
        self.behavior = "ok"
        # the format the mock emits; the provider only supports 16-bit mono
        self.width = WIDTH
        self.channels = CHANNELS
        # (served program, voice, text) of every synthesize request served
        self.synthesized: list[tuple[str, str, str]] = []

    def info_event(self) -> Event:
        """Return the info answer to a describe request."""
        attribution = Attribution(name="mock", url="https://example.com/mock")
        return Info(
            tts=[
                TtsProgram(
                    name=program.name,
                    attribution=attribution,
                    installed=program.installed,
                    description=None,
                    version="1.0.0",
                    voices=[
                        TtsVoice(
                            name=voice.name,
                            attribution=attribution,
                            installed=voice.installed,
                            description=voice.description,
                            version=None,
                            languages=list(voice.languages),
                        )
                        for voice in program.voices
                    ],
                )
                for program in self.programs
            ]
        ).event()


class MockClient:
    """One provider connection, served in memory by the state of a MockServer."""

    def __init__(
        self,
        server: MockServer,
        connect_timeout: float | None = None,
        read_timeout: float | None = None,
    ) -> None:
        """
        Initialize the client.

        :param server: The state the connection is served by.
        :param connect_timeout: Kept like the real client, unused in memory.
        :param read_timeout: Kept like the real client, unused in memory.
        """
        self._server = server
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._selected: str | None = None
        self._queue: asyncio.Queue[Event] = asyncio.Queue()

    async def __aenter__(self) -> Self:
        """Connect; the mock has no connection to establish."""
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Disconnect; there is nothing to clean up."""

    async def write_event(self, event: Event) -> None:
        """Serve one event the provider writes."""
        if Describe.is_type(event.type):
            self._queue.put_nowait(self._server.info_event())
        elif SelectProgram.is_type(event.type):
            self._selected = SelectProgram.from_event(event).name
        elif Synthesize.is_type(event.type):
            self._synthesize(Synthesize.from_event(event))

    async def read_event(self) -> Event | None:
        """Return the next event for the provider, as the network would deliver it."""
        return await self._queue.get()

    def _synthesize(self, request: Synthesize) -> None:
        """Serve a synthesize request on the connection."""
        server = self._server
        # a server falls back to its first program when the selected one is unknown
        program = next(
            (candidate for candidate in server.programs if candidate.name == self._selected),
            server.programs[0],
        )
        voice_name = "" if request.voice is None else request.voice.name or ""
        server.synthesized.append((program.name, voice_name, request.text))
        if server.behavior == "error":
            self._queue.put_nowait(Error(text="synthesis failed", code="500").event())
        elif server.behavior == "no_audio":
            self._queue.put_nowait(AudioStop().event())
        elif server.behavior != "silent":
            size = SAMPLES_PER_CHUNK * server.width * server.channels
            for offset in range(0, len(AUDIO), size):
                self._queue.put_nowait(
                    AudioChunk(
                        rate=RATE,
                        width=server.width,
                        channels=server.channels,
                        audio=AUDIO[offset : offset + size],
                    ).event()
                )
            self._queue.put_nowait(AudioStop().event())


class MockClientFactory:
    """
    The stand-in for ``AsyncClient.from_uri``: routes each connection to its mock server.

    A test creates the servers it wants with :meth:`create`; an address without a server is
    refused, like one where nothing listens.
    """

    def __init__(self) -> None:
        """Initialize the factory without any server."""
        self._servers: dict[str, MockServer] = {}

    def create(self, programs: list[Program], uri: str = "tcp://127.0.0.1:10200") -> MockServer:
        """
        Create a mock server at the given address.

        :param programs: The TTS programs it advertises, in the order advertised.
        :param uri: The address clients connect to it at.
        """
        server = MockServer(programs, uri)
        self._servers[uri] = server
        return server

    def from_uri(
        self, uri: str, connect_timeout: float | None = None, read_timeout: float | None = None
    ) -> MockClient:
        """
        Return the client for the given address, refusing one where nothing listens.

        :param uri: The tcp:// address to connect to.
        :param connect_timeout: Passed through to the client, like the real client.
        :param read_timeout: Passed through to the client, like the real client.
        """
        if uri not in self._servers:
            raise OSError("Connection refused")
        return MockClient(self._servers[uri], connect_timeout, read_timeout)
