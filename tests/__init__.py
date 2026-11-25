"""The shared pieces of the test suite: the provider's names and the helpers for its tests."""

from pathlib import Path

from music_assistant_models.enums import FlowStepType

from music_assistant.mass import MusicAssistant
from music_assistant.models.plugin import PluginProvider

# the server imports the provider as ``music_assistant.providers.wyoming_tts``, a different
# module object than the ``wyoming_tts`` package of the repository: only the plain strings
# may come from it, never its classes
from wyoming_tts.const import CONF_SERVER_URI

# the domain of the provider, which names its folder
DOMAIN = "wyoming_tts"
REPOSITORY = Path(__file__).parents[1]


async def add_provider(mass: MusicAssistant, server_uri: str) -> str:
    """Run the setup flow against the given server and return the new instance id."""
    step = await mass.config.setup_provider(DOMAIN)
    assert step.type == FlowStepType.FORM
    step = await mass.config.submit_setup_flow(step.flow_id, {CONF_SERVER_URI: server_uri})
    assert step.type == FlowStepType.FINISH, step
    assert step.result is not None
    return step.result["instance_id"]


def provider_of(mass: MusicAssistant, instance_id: str) -> PluginProvider:
    """Return the loaded provider of the given instance, narrowed to the shared base class."""
    provider = mass.get_provider(instance_id, provider_type=PluginProvider)
    assert isinstance(provider, PluginProvider)
    return provider
