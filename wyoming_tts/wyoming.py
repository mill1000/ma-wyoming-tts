"""
The Wyoming protocol layer of the provider.

The model of the voices a Wyoming TTS server advertises, the matching of the
provider's voice filters against them, the discovery of the voices, and the
rendering of messages with them.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from fnmatch import fnmatch

from music_assistant_models.errors import MusicAssistantError
from wyoming.audio import AudioChunk, AudioStop
from wyoming.client import AsyncClient
from wyoming.error import Error
from wyoming.info import Describe, Info, SelectProgram
from wyoming.tts import Synthesize, SynthesizeVoice

# discovery runs during provider load, so it gives up quickly when the server is down
CONNECT_TIMEOUT = 10
# timeout for one event during a synthesis; the overall deadline is the synthesis timeout
READ_TIMEOUT = 30


class NoTtsVoicesError(MusicAssistantError):
    """The Wyoming server answered, but advertises no usable TTS voices."""


class SynthesisError(MusicAssistantError):
    """The Wyoming server sent an error event, or emitted unusable audio."""


class IncompleteSynthesisError(MusicAssistantError):
    """The Wyoming server closed the connection before the speech was complete."""


def normalize_server_uri(value: str) -> str:
    """
    Return the Wyoming server address for the value entered by the user.

    Accepts a tcp:// address, or a bare host:port, which is taken to be tcp.

    :param value: The server address as entered by the user.
    """
    value = value.strip()
    if "://" not in value:
        value = f"tcp://{value}"
    return value


@dataclass(frozen=True, slots=True)
class DiscoveredVoice:
    """A program/voice pair the Wyoming server advertises."""

    program: str
    voice: str
    languages: tuple[str, ...]
    description: str | None


def voice_matches(matcher: str, program: str, voice: str) -> bool:
    """
    Return whether a voice filter value selects the given program/voice pair.

    The value is an fnmatch pattern matched against the engine id, the voice name
    prefixed with the program name and a colon (e.g. ``piper:en_US-a``): ``piper*``
    selects all the voices of program ``piper``, ``*nl_NL-*`` the Dutch voices of any
    program, and ``*`` everything.

    :param matcher: One value of the voice filter.
    :param program: The name of a discovered program.
    :param voice: The name of a discovered voice.
    """
    return fnmatch(f"{program}:{voice}", matcher)


def voice_language_matches(matcher: str, languages: tuple[str, ...]) -> bool:
    """
    Return whether a language filter value selects any of the voice's languages.

    The value is an fnmatch pattern matched against each language code the voice
    advertises. A pattern containing a hyphen (e.g. ``en-*``) selects the base
    language and all of its variants, so it also matches the bare code ``en``;
    a value without a hyphen (e.g. ``en``) matches exactly.

    :param matcher: One value of the language filter.
    :param languages: The language codes of a discovered voice.
    """
    if "-" in matcher:
        matcher = f"{matcher.partition('-')[0]}*"
    return any(fnmatch(language, matcher) for language in languages)


async def discover_voices(server_uri: str) -> list[DiscoveredVoice]:
    """
    Connect to the Wyoming server and return the TTS voices it advertises.

    :param server_uri: The tcp:// address of the server.
    :raises NoTtsVoicesError: When the server advertises no TTS program with voices.
    """
    async with AsyncClient.from_uri(
        server_uri, connect_timeout=CONNECT_TIMEOUT, read_timeout=CONNECT_TIMEOUT
    ) as client:
        await client.write_event(Describe().event())
        event = await client.read_event()
    if event is None or not Info.is_type(event.type):
        msg = "The Wyoming server did not answer the describe request"
        raise NoTtsVoicesError(msg)
    info = Info.from_event(event)
    voices = [
        DiscoveredVoice(
            program=program.name,
            voice=voice.name,
            languages=tuple(voice.languages or ()),
            description=voice.description,
        )
        for program in info.tts
        if program.installed
        for voice in program.voices
        if voice.installed
    ]
    if not voices:
        msg = "The Wyoming server does not advertise any TTS voices"
        raise NoTtsVoicesError(msg)
    return voices


async def synthesize(
    server_uri: str, program: str, voice: str, message: str, timeout: float
) -> tuple[bytes, int]:
    """
    Render a message with the given program and voice of the given server.

    Opens a fresh connection for the call and collects the audio until the server
    stops it.

    :param server_uri: The tcp:// address of the server.
    :param program: The name of the TTS program to synthesize with.
    :param voice: The name of the voice within that program.
    :param message: The text to convert to speech.
    :param timeout: The overall deadline in seconds.
    :return: The rendered 16-bit little-endian mono PCM and its sample rate in Hz.
    :raises SynthesisError: When the server sent an error event or unusable audio.
    :raises IncompleteSynthesisError: When the connection closed before the stop.
    """
    pcm = bytearray()
    rate = 0
    async with asyncio.timeout(timeout):
        async with AsyncClient.from_uri(
            server_uri, connect_timeout=CONNECT_TIMEOUT, read_timeout=READ_TIMEOUT
        ) as client:
            # harmless on single-program servers, which fall back to their first program
            await client.write_event(SelectProgram(name=program).event())
            await client.write_event(
                Synthesize(text=message, voice=SynthesizeVoice(name=voice)).event()
            )
            while event := await client.read_event():
                if AudioChunk.is_type(event.type):
                    chunk = AudioChunk.from_event(event)
                    if not rate:
                        # every known Wyoming TTS server emits 16-bit little-endian mono
                        if chunk.width != 2 or chunk.channels != 1:
                            msg = (
                                f"The Wyoming server emitted {chunk.width * 8}-bit "
                                f"{chunk.channels}-channel audio, only 16-bit mono is supported"
                            )
                            raise SynthesisError(msg)
                        rate = chunk.rate
                    pcm += chunk.audio
                elif AudioStop.is_type(event.type):
                    if not pcm:
                        msg = "The Wyoming server rendered no audio for the message"
                        raise IncompleteSynthesisError(msg)
                    return bytes(pcm), rate
                elif Error.is_type(event.type):
                    err = Error.from_event(event)
                    detail = err.code or "unknown"
                    msg = f"The Wyoming server refused to render: {err.text} ({detail})"
                    raise SynthesisError(msg)
    msg = "The Wyoming server stopped before the speech was rendered"
    raise IncompleteSynthesisError(msg)
