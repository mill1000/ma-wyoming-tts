"""Unit tests of the Wyoming protocol layer, in memory, without a server."""

import pytest

from tests.mock_client import AUDIO, RATE, MockClientFactory, Program, Voice
from wyoming_tts.wyoming import (
    DiscoveredVoice,
    IncompleteSynthesisError,
    NoTtsVoicesError,
    SynthesisError,
    discover_voices,
    normalize_server_uri,
    synthesize,
    voice_language_matches,
    voice_matches,
)

# a single-program server with one voice
PROGRAMS = [Program("piper", (Voice("en_US-a"),))]


def test_normalize_server_uri() -> None:
    """A bare host:port is taken to be tcp, and whitespace is stripped."""
    assert normalize_server_uri("192.168.1.50:10200") == "tcp://192.168.1.50:10200"
    assert normalize_server_uri("tcp://192.168.1.50:10200") == "tcp://192.168.1.50:10200"
    assert normalize_server_uri("  tcp://127.0.0.1:10200  ") == "tcp://127.0.0.1:10200"


def test_voice_matches() -> None:
    """The voice filter matches the voice name prefixed with the program name."""
    # the pattern is a glob against the engine id, program and voice with a colon
    assert voice_matches("piper:en_US-*", "piper", "en_US-a")
    assert not voice_matches("piper:en_US-*", "kitten", "en_US-a")
    assert not voice_matches("piper:nl_NL-*", "piper", "en_US-a")
    # without a wildcard the pattern is an exact match on the whole id: a bare program
    # name selects nothing
    assert voice_matches("piper:en_US-a", "piper", "en_US-a")
    assert not voice_matches("piper", "piper", "anything")
    # a leading wildcard reaches the voice names of any program
    assert voice_matches("*en_US-*", "kitten", "en_US-b")
    assert not voice_matches("*en_US-*", "kitten", "nl_NL-b")
    # * matches everything
    assert voice_matches("*", "piper", "en_US-a")


def test_voice_language_matches() -> None:
    """A language matcher matches exactly, or the base language and all its variants."""
    assert voice_language_matches("en", ("en",))
    assert not voice_language_matches("en", ("en_GB",))
    # a pattern with a hyphen selects the base language and all of its variants
    assert voice_language_matches("en-*", ("en",))
    assert voice_language_matches("en-*", ("en_GB",))
    assert not voice_language_matches("en-*", ("de",))
    assert voice_language_matches("cmn-*", ("cmn", "de"))
    assert not voice_language_matches("fr-*", ("cmn", "de"))


async def test_discover_returns_the_advertised_voices(mock_client: MockClientFactory) -> None:
    """Discovery returns the installed voices in advertised order, with their languages."""
    server = mock_client.create(
        [
            Program("piper", (Voice("en_US-a"), Voice("gone", installed=False))),
            Program("kitten", (Voice("nl_NL-b", languages=("nl",), description="Bert"),)),
            Program("pocket", (Voice("de-DE-c", languages=("de",)),), installed=False),
        ]
    )
    voices = await discover_voices(server.uri)
    assert voices == [
        DiscoveredVoice("piper", "en_US-a", ("en",), None),
        DiscoveredVoice("kitten", "nl_NL-b", ("nl",), "Bert"),
    ]


async def test_discover_refuses_a_server_without_voices(mock_client: MockClientFactory) -> None:
    """A server that advertises no usable TTS voice fails discovery with a reason."""
    server = mock_client.create([Program("piper", (Voice("en_US-a", installed=False),))])
    with pytest.raises(NoTtsVoicesError, match="does not advertise any"):
        await discover_voices(server.uri)
    # and so does one that advertises no program at all
    server = mock_client.create([])
    with pytest.raises(NoTtsVoicesError, match="does not advertise any"):
        await discover_voices(server.uri)


async def test_discover_raises_for_an_unreachable_address() -> None:
    """An address with no server behind it fails discovery with a connection error."""
    with pytest.raises(OSError, match="Connection refused"):
        await discover_voices("tcp://127.0.0.1:1")


async def test_synthesize_returns_the_rendered_pcm(mock_client: MockClientFactory) -> None:
    """Synthesis collects the audio until the stop and returns it with its rate."""
    server = mock_client.create(PROGRAMS)
    pcm, rate = await synthesize(server.uri, "piper", "en_US-a", "Hello", timeout=5)
    assert (pcm, rate) == (AUDIO, RATE)
    assert server.synthesized == [("piper", "en_US-a", "Hello")]


async def test_synthesize_selects_the_requested_program(mock_client: MockClientFactory) -> None:
    """Each render selects its own program on the server."""
    server = mock_client.create(
        [
            Program("piper", (Voice("en_US-a"),)),
            Program("kitten", (Voice("en_US-b"),)),
        ]
    )
    await synthesize(server.uri, "kitten", "en_US-b", "Hello", timeout=5)
    assert server.synthesized == [("kitten", "en_US-b", "Hello")]
    # an unknown program falls back to the server's first one
    await synthesize(server.uri, "unknown", "en_US-a", "Again", timeout=5)
    assert server.synthesized[-1] == ("piper", "en_US-a", "Again")


async def test_synthesize_raises_on_an_error_event(mock_client: MockClientFactory) -> None:
    """An error event from the server surfaces with its text and code."""
    server = mock_client.create(PROGRAMS)
    server.behavior = "error"
    with pytest.raises(SynthesisError, match="synthesis failed \\(500\\)"):
        await synthesize(server.uri, "piper", "en_US-a", "Hello", timeout=5)


async def test_synthesize_raises_on_a_stop_without_audio(mock_client: MockClientFactory) -> None:
    """A stop with no audio before it is an incomplete synthesis."""
    server = mock_client.create(PROGRAMS)
    server.behavior = "no_audio"
    with pytest.raises(IncompleteSynthesisError, match="rendered no audio"):
        await synthesize(server.uri, "piper", "en_US-a", "Hello", timeout=5)


async def test_synthesize_times_out_on_a_silent_server(mock_client: MockClientFactory) -> None:
    """A server that never answers is failed at the deadline."""
    server = mock_client.create(PROGRAMS)
    server.behavior = "silent"
    with pytest.raises(TimeoutError):
        await synthesize(server.uri, "piper", "en_US-a", "Hello", timeout=0.5)


async def test_synthesize_refuses_unsupported_audio(mock_client: MockClientFactory) -> None:
    """Only 16-bit mono is supported; anything else fails with a reason."""
    server = mock_client.create(PROGRAMS)
    server.channels = 2
    with pytest.raises(SynthesisError, match="2-channel"):
        await synthesize(server.uri, "piper", "en_US-a", "Hello", timeout=5)
    server.channels = 1
    server.width = 4
    with pytest.raises(SynthesisError, match="32-bit"):
        await synthesize(server.uri, "piper", "en_US-a", "Hello", timeout=5)
