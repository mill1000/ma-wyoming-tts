"""Setup flow for the Wyoming Text-to-speech provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.setup_flow import SetupFlowError

from .const import CONF_SERVER_URI
from .wyoming import NoTtsVoicesError, discover_voices, normalize_server_uri

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

# a placeholder address the user edits; a port where nothing listens, so a blind submit fails
DEFAULT_SERVER_URI = "tcp://127.0.0.1:10200"
# the only scheme the provider can connect to; stdio would require the server to spawn and
# supervise a process, which is out of scope
SUPPORTED_SCHEME = "tcp://"


async def run_setup(session: SetupSession) -> None:
    """
    Run the Wyoming Text-to-speech setup flow.

    Collects the address of the Wyoming TTS server, then verifies it by discovering its
    voices.

    :param session: The setup session driving the flow.
    """
    setup_data = dict(session.context.setup_data)
    errors: dict[str, str | SetupFlowError] | None = None
    while True:
        values = await session.form(
            [
                ConfigEntry(
                    key=CONF_SERVER_URI,
                    type=ConfigEntryType.STRING,
                    required=True,
                    default_value=DEFAULT_SERVER_URI,
                    value=setup_data.get(CONF_SERVER_URI),
                ),
            ],
            step_id="user",
            errors=errors,
            last_step=True,
        )
        setup_data.update(values)
        server_uri = normalize_server_uri(str(values[CONF_SERVER_URI]))
        if not server_uri.startswith(SUPPORTED_SCHEME):
            errors = {
                CONF_SERVER_URI: SetupFlowError(
                    "Only tcp://host:port addresses are supported",
                    "invalid_uri",
                )
            }
            continue
        try:
            await discover_voices(server_uri)
        except NoTtsVoicesError as err:
            errors = {CONF_SERVER_URI: SetupFlowError(str(err), "no_tts_programs")}
            continue
        except OSError as err:
            errors = {
                CONF_SERVER_URI: SetupFlowError(f"Connection failed: {err}", "cannot_connect")
            }
            continue
        try:
            await session.finish({CONF_SERVER_URI: server_uri})
        except SetupFlowError:
            # the instance could not be created; re-render the form so the user can retry
            errors = {"base": SetupFlowError("The provider could not be created")}
            continue
        return
