"""Tests of the provider, loaded by a real server."""

import io
from pathlib import Path

import av
import pytest
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    FlowStepType,
    MediaType,
    ProviderFeature,
    ProviderTier,
    StreamType,
)
from music_assistant_models.errors import MusicAssistantError

from music_assistant.constants import CONF_PROVIDERS
from music_assistant.mass import MusicAssistant
from music_assistant.models.plugin import PluginProvider
from tests import DOMAIN
from tests.mock_client import AUDIO, RATE, MockClientFactory, Program, Voice

# the server imports the provider as ``music_assistant.providers.wyoming_tts``, a different
# module object than the ``wyoming_tts`` package of the repository: only the plain strings
# may come from it, never its classes
from wyoming_tts.const import (
    CONF_OUTPUT_FORMAT,
    CONF_SERVER_URI,
    CONF_VOICE_LANGUAGES,
    CONF_VOICES,
)

# a single-program server with one voice, the shape of most wyoming-piper setups
SINGLE_PROGRAM = [Program("piper", (Voice("en_US-a"),))]


async def _add_provider(mass: MusicAssistant, server_uri: str) -> str:
    """Run the setup flow against the given server and return the new instance id."""
    step = await mass.config.setup_provider(DOMAIN)
    assert step.type == FlowStepType.FORM
    step = await mass.config.submit_setup_flow(step.flow_id, {CONF_SERVER_URI: server_uri})
    assert step.type == FlowStepType.FINISH, step
    assert step.result is not None
    return step.result["instance_id"]


def _provider(mass: MusicAssistant, instance_id: str) -> PluginProvider:
    """Return the loaded provider of the given instance, narrowed to the shared base class."""
    provider = mass.get_provider(instance_id, provider_type=PluginProvider)
    assert isinstance(provider, PluginProvider)
    return provider


def _duration_seconds(audio: bytes) -> float:
    """Return the duration of a rendered clip in seconds."""
    # av.open returns an input or output container; only the input has a duration
    with av.open(io.BytesIO(audio)) as container:
        duration = getattr(container, "duration", None)
    assert duration is not None
    return float(duration) / av.time_base


async def test_provider_loads(mass: MusicAssistant) -> None:
    """The server loads the provider from this repository as a community provider."""
    assert mass.get_provider_manifest(DOMAIN).tier == ProviderTier.COMMUNITY


async def test_setup_flow_validates_and_exposes_engines(
    mass: MusicAssistant, mock_client: MockClientFactory
) -> None:
    """The setup flow verifies the server and the provider exposes one engine per voice."""
    server = mock_client.create(
        [Program("piper", (Voice("en_US-a"), Voice("nl_NL-b", languages=("nl",))))]
    )
    instance_id = await _add_provider(mass, server.uri)
    provider = _provider(mass, instance_id)
    assert provider.available
    assert ProviderFeature.TTS in provider.supported_features
    engines = await provider.get_tts_engines()
    assert [engine.id for engine in engines] == ["piper:en_US-a", "piper:nl_NL-b"]
    # the program always prefixes the name, so the choice says where it renders
    assert [engine.name for engine in engines] == ["piper / en_US-a", "piper / nl_NL-b"]
    assert all(engine.provider is provider for engine in engines)
    # with several instances, the name postfix is the server address, not a number
    assert provider.instance_name_postfix == server.uri.removeprefix("tcp://")
    # the options page shows the voices that are available (none filtered here)
    entries = await provider.get_config_entries()
    label = next(entry for entry in entries if entry.key == "available_voices")
    assert label.type == ConfigEntryType.LABEL
    assert label.translation_params == ["piper: en_US-a (en), nl_NL-b (nl)"]


async def test_setup_flow_rejects_bad_uri(mass: MusicAssistant) -> None:
    """An unreachable or unsupported address comes back as a field error on the form."""
    step = await mass.config.setup_provider(DOMAIN)
    assert step.type == FlowStepType.FORM
    # nothing listens on this port
    step = await mass.config.submit_setup_flow(step.flow_id, {CONF_SERVER_URI: "tcp://127.0.0.1:1"})
    assert step.type == FlowStepType.FORM
    assert (step.errors.get(CONF_SERVER_URI) or "").startswith("Connection failed")
    # an unsupported scheme is refused without a connection attempt
    step = await mass.config.submit_setup_flow(step.flow_id, {CONF_SERVER_URI: "stdio://cat"})
    assert step.type == FlowStepType.FORM
    assert "tcp://host:port" in (step.errors.get(CONF_SERVER_URI) or "")


async def test_get_tts_message_streams_audio(
    mass: MusicAssistant, mock_client: MockClientFactory
) -> None:
    """A rendered message is served over http and cached between calls."""
    server = mock_client.create(SINGLE_PROGRAM)
    instance_id = await _add_provider(mass, server.uri)
    provider = _provider(mass, instance_id)
    details = await provider.get_tts_message("Hello", engine_id="piper:en_US-a")
    assert details.stream_type == StreamType.HTTP
    assert details.media_type == MediaType.SOUND_EFFECT
    assert details.audio_format.content_type == ContentType.MP3
    path = str(details.path)
    assert path.startswith(mass.streams.base_url)
    async with mass.http_session.get(path) as response:
        assert response.status == 200
        audio = await response.read()
    assert len(audio) > 0
    assert _duration_seconds(audio) == pytest.approx(1.0, abs=0.3)
    # the same message reuses the cached clip
    details_again = await provider.get_tts_message("Hello", engine_id="piper:en_US-a")
    assert str(details_again.path) == path
    async with mass.http_session.get(str(details_again.path)) as response:
        assert await response.read() == audio
    # a different message renders a different clip
    details_other = await provider.get_tts_message("Goodbye", engine_id="piper:en_US-a")
    assert str(details_other.path) != path
    # one render per distinct message: "Hello" once (the second call is a cache hit)
    # and "Goodbye" once
    assert len(server.synthesized) == 2


async def test_multi_program_server_selects_program(
    mass: MusicAssistant, mock_client: MockClientFactory
) -> None:
    """Each engine selects its own program on the server."""
    server = mock_client.create(
        [
            Program("piper", (Voice("en_US-a"),)),
            Program("kitten", (Voice("en_US-b"),)),
        ]
    )
    instance_id = await _add_provider(mass, server.uri)
    provider = _provider(mass, instance_id)
    engines = await provider.get_tts_engines()
    assert [(engine.id, engine.name) for engine in engines] == [
        ("piper:en_US-a", "piper / en_US-a"),
        ("kitten:en_US-b", "kitten / en_US-b"),
    ]
    await provider.get_tts_message("Hello", engine_id="kitten:en_US-b")
    assert server.synthesized[-1] == ("kitten", "en_US-b", "Hello")
    # without an engine id the first engine is used
    await provider.get_tts_message("Hello again")
    assert server.synthesized[-1] == ("piper", "en_US-a", "Hello again")


async def test_synthesis_error_surfaced(
    mass: MusicAssistant, mock_client: MockClientFactory
) -> None:
    """An error event from the server surfaces as a MusicAssistantError."""
    server = mock_client.create(SINGLE_PROGRAM)
    instance_id = await _add_provider(mass, server.uri)
    provider = _provider(mass, instance_id)
    server.behavior = "error"
    with pytest.raises(MusicAssistantError, match="synthesis failed"):
        await provider.get_tts_message("Hello", engine_id="piper:en_US-a")
    # the failed render is not cached: a recovered server renders the message
    server.behavior = "ok"
    details = await provider.get_tts_message("Hello", engine_id="piper:en_US-a")
    assert len(str(details.path)) > 0


async def test_wav_format(mass: MusicAssistant, mock_client: MockClientFactory) -> None:
    """The output format option changes how clips are packaged, without a reload."""
    server = mock_client.create(SINGLE_PROGRAM)
    instance_id = await _add_provider(mass, server.uri)
    provider = _provider(mass, instance_id)
    await mass.config.save_provider_config(
        DOMAIN, {CONF_OUTPUT_FORMAT: "wav"}, instance_id=instance_id
    )
    details = await provider.get_tts_message("Hello", engine_id="piper:en_US-a")
    assert details.audio_format.content_type == ContentType.WAV
    async with mass.http_session.get(str(details.path)) as response:
        assert response.status == 200
        audio = await response.read()
    assert audio.startswith(b"RIFF")
    assert audio[8:12] == b"WAVE"
    assert _duration_seconds(audio) == pytest.approx(1.0, abs=0.05)


async def test_pcm_format_serves_raw_audio(
    mass: MusicAssistant, mock_client: MockClientFactory
) -> None:
    """Raw pcm output serves the server's unmodified bytes, with the rate in the content type."""
    server = mock_client.create(SINGLE_PROGRAM)
    instance_id = await _add_provider(mass, server.uri)
    provider = _provider(mass, instance_id)
    await mass.config.save_provider_config(
        DOMAIN, {CONF_OUTPUT_FORMAT: "pcm"}, instance_id=instance_id
    )
    details = await provider.get_tts_message("Hello", engine_id="piper:en_US-a")
    assert details.audio_format.content_type == ContentType.PCM_S16LE
    assert details.audio_format.sample_rate == RATE
    async with mass.http_session.get(str(details.path)) as response:
        assert response.status == 200
        assert response.headers["Content-Type"] == f"audio/L16;rate={RATE};channels=1"
        audio = await response.read()
    # the clip is exactly the raw pcm the server sent: no header, no re-encoding
    assert audio == AUDIO


async def test_voice_filter(mass: MusicAssistant, mock_client: MockClientFactory) -> None:
    """The voice filter limits the exposed engines, applied live without a reload."""
    programs = [
        Program(
            "piper",
            (Voice("en_US-a"), Voice("en_US-b"), Voice("nl_NL-c", languages=("nl",))),
        ),
        Program("kitten", (Voice("en_US-x"),)),
    ]
    all_ids = ["piper:en_US-a", "piper:en_US-b", "piper:nl_NL-c", "kitten:en_US-x"]
    server = mock_client.create(programs)
    instance_id = await _add_provider(mass, server.uri)
    provider = _provider(mass, instance_id)

    async def engine_ids() -> list[str]:
        return [engine.id for engine in await provider.get_tts_engines()]

    assert await engine_ids() == all_ids
    for matchers, expected in [
        (["en_US-*"], ["piper:en_US-a", "piper:en_US-b", "kitten:en_US-x"]),
        (["piper:nl_NL-c"], ["piper:nl_NL-c"]),
        (["kitten"], ["kitten:en_US-x"]),
        ([], all_ids),
    ]:
        await mass.config.save_provider_config(
            DOMAIN, {CONF_VOICES: matchers}, instance_id=instance_id
        )
        assert await engine_ids() == expected, matchers
    # a combination that matches nothing is a legal state: it saves, exposes no
    # engine, and the available-voices entry says so
    await mass.config.save_provider_config(
        DOMAIN, {CONF_VOICES: ["zzz_*"]}, instance_id=instance_id
    )
    assert await engine_ids() == []
    entries = await _provider(mass, instance_id).get_config_entries()
    label = next(entry for entry in entries if entry.key == "available_voices")
    assert label.translation_params == ["none available"]
    # clearing the filter restores the engines
    await mass.config.save_provider_config(DOMAIN, {CONF_VOICES: []}, instance_id=instance_id)
    assert await engine_ids() == all_ids


async def test_dead_end_filter_at_load_loads_without_engines(
    mass: MusicAssistant, mock_client: MockClientFactory
) -> None:
    """A stored dead-end filter leaves no engines; fixing the options restores them."""
    server = mock_client.create(SINGLE_PROGRAM)
    instance_id = await _add_provider(mass, server.uri)
    # write a dead-end value directly, as when the server stops advertising
    # the voices it matched
    mass.config.set(f"{CONF_PROVIDERS}/{instance_id}/values", {"voices": ["zzz_*"]})
    await mass.load_provider(instance_id)
    provider = _provider(mass, instance_id)
    assert await provider.get_tts_engines() == []
    with pytest.raises(MusicAssistantError, match="no voice matching"):
        await provider.get_tts_message("Hello")
    # fixing the filter from the options page restores the engines
    await mass.config.save_provider_config(DOMAIN, {CONF_VOICES: []}, instance_id=instance_id)
    provider = _provider(mass, instance_id)
    assert [engine.id for engine in await provider.get_tts_engines()] == ["piper:en_US-a"]


async def test_voice_language_filter(mass: MusicAssistant, mock_client: MockClientFactory) -> None:
    """The language filter limits the exposed engines by spoken language."""
    programs = [
        Program(
            "piper",
            (
                Voice("en_US-a", languages=("en",)),
                Voice("cmn-CN-a", languages=("cmn",)),
                Voice("en_GB-b", languages=("en", "de")),
            ),
        ),
    ]
    all_ids = ["piper:en_US-a", "piper:cmn-CN-a", "piper:en_GB-b"]
    server = mock_client.create(programs)
    instance_id = await _add_provider(mass, server.uri)
    provider = _provider(mass, instance_id)

    async def engine_ids() -> list[str]:
        return [engine.id for engine in await provider.get_tts_engines()]

    assert await engine_ids() == all_ids
    for matchers, expected in [
        (["en"], ["piper:en_US-a", "piper:en_GB-b"]),
        (["cmn-*"], ["piper:cmn-CN-a"]),
        (["en", "de"], ["piper:en_US-a", "piper:en_GB-b"]),
        ([], all_ids),
    ]:
        await mass.config.save_provider_config(
            DOMAIN, {CONF_VOICE_LANGUAGES: matchers}, instance_id=instance_id
        )
        assert await engine_ids() == expected, matchers
    # the name and language filters combine: a voice must pass both
    await mass.config.save_provider_config(
        DOMAIN, {CONF_VOICE_LANGUAGES: ["en"]}, instance_id=instance_id
    )
    await mass.config.save_provider_config(
        DOMAIN, {CONF_VOICES: ["*_GB-*"]}, instance_id=instance_id
    )
    assert await engine_ids() == ["piper:en_GB-b"]
    # a combination that matches nothing exposes no engine until fixed
    await mass.config.save_provider_config(
        DOMAIN, {CONF_VOICES: ["*_CN-*"]}, instance_id=instance_id
    )
    assert await engine_ids() == []
    await mass.config.save_provider_config(
        DOMAIN, {CONF_VOICES: ["*_GB-*"]}, instance_id=instance_id
    )
    assert await engine_ids() == ["piper:en_GB-b"]
    # the available-voices label tracks the filters, no reload needed
    entries = await provider.get_config_entries()
    label = next(entry for entry in entries if entry.key == "available_voices")
    assert label.translation_params == ["piper: en_GB-b (en, de)"]
    await mass.config.save_provider_config(
        DOMAIN,
        {CONF_VOICE_LANGUAGES: ["cmn"], CONF_VOICES: []},
        instance_id=instance_id,
    )
    entries = await provider.get_config_entries()
    label = next(entry for entry in entries if entry.key == "available_voices")
    assert label.translation_params == ["piper: cmn-CN-a (cmn)"]


async def test_unload_unregisters_route_and_removes_cache(
    mass: MusicAssistant, mock_client: MockClientFactory
) -> None:
    """Removing the provider unregisters its route and removes its cache directory."""
    server = mock_client.create(SINGLE_PROGRAM)
    instance_id = await _add_provider(mass, server.uri)
    provider = _provider(mass, instance_id)
    details = await provider.get_tts_message("Hello", engine_id="piper:en_US-a")
    path = str(details.path)
    # the cache lives under the mass cache path, per domain and instance
    cache_dir = Path(mass.cache_path) / DOMAIN / instance_id
    assert cache_dir.is_dir()
    await mass.config.remove_provider_config(instance_id)
    assert not cache_dir.is_dir()
    async with mass.http_session.get(path) as response:
        assert response.status == 404
