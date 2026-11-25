"""Tests of the setup flow, which collects the server address and verifies it."""

from music_assistant_models.enums import ConfigEntryType, FlowStepType, ProviderFeature

from music_assistant.mass import MusicAssistant
from tests import DOMAIN, add_provider, provider_of
from tests.mock_client import MockClientFactory, Program, Voice
from wyoming_tts.const import CONF_SERVER_URI


async def test_setup_flow_validates_and_exposes_engines(
    mass: MusicAssistant, mock_client: MockClientFactory
) -> None:
    """The setup flow verifies the server and the provider exposes one engine per voice."""
    server = mock_client.create(
        [Program("piper", (Voice("en_US-a"), Voice("nl_NL-b", languages=("nl",))))]
    )
    instance_id = await add_provider(mass, server.uri)
    provider = provider_of(mass, instance_id)
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
    assert step.errors.get(CONF_SERVER_URI) == (
        "Could not connect to the server. Check the address and ensure the server is running."
    )
    # an unsupported scheme is refused without a connection attempt
    step = await mass.config.submit_setup_flow(step.flow_id, {CONF_SERVER_URI: "stdio://cat"})
    assert step.type == FlowStepType.FORM
    assert step.errors.get(CONF_SERVER_URI) == "Invalid server address. Expected host:port"
