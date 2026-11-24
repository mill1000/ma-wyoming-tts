# Wyoming TTS for Music Assistant

A [Music Assistant](https://music-assistant.io) community provider that exposes
[Wyoming](https://github.com/rhasspy/wyoming) protocol TTS servers —
[wyoming-piper](https://github.com/rhasspy/wyoming-piper),
[wyoming-tts](https://github.com/.../wyoming-tts), ESPHome with the wyoming TTS component,
and the like — as standard Music Assistant **text-to-speech engines**, so the features
that speak can use a local, private voice:

- **Announcements** — `mass.players.announce(player, message=...)`, or the
  "Announcement text-to-speech engine" setting under Settings → System → Players.
- **AI Radio** — the DJ clips of the AI Radio providers.

Each TTS program/voice the server advertises in its `describe` answer becomes one
selectable engine; add one provider instance per Wyoming server.

## Supported servers

Any server that speaks the Wyoming TTS protocol over a TCP socket:

| Server | Notes |
| --- | --- |
| [wyoming-piper](https://github.com/rhasspy/wyoming-piper) | The reference Piper server; one program per onnx model |
| a multi-backend Wyoming TTS server | Servers that combine several backends (pocket, kitten, ...) expose one program per backend |
| [ESPHome](https://esphome.io) | Devices with the `wyoming` component |

The audio must be 16-bit little-endian mono PCM (what every known Wyoming TTS server
emits). The sample rate of the server is preserved in the rendered clips; the players
resample as needed.

### Security

The Wyoming protocol has **no authentication and no encryption by design**; it is meant
for a trusted network. Run the TTS server on your LAN, the same way you would run
wyoming-piper for Home Assistant.

## Installation

The provider is a plugin: it runs inside the Music Assistant server, so you need a
server that can load community providers.

- **Home Assistant add-on:** the add-on's `PROVIDER_REPO` environment variable has been
  replaced by a dev-mode server with this repository as its provider folder — use that.
  On a standard server it will be installable from the
  [community catalog](https://github.com/music-assistant/community-catalog) once listed.
- **Any other server:** start it with this repository as a local provider folder, as the
  development setup below does.

Then add the provider under Settings → Plugins and enter the address of your Wyoming
TTS server:

- `tcp://192.168.1.100:10200` for a server on the network — a bare `192.168.1.100:10200`
  works too.

The setup verifies the server answers with at least one TTS voice before the provider
is added, so a misconfigured server fails fast with a reason instead of surfacing
during an announcement.

### Options

| Option | Default | Meaning |
| --- | --- | --- |
| Voice filter | *(empty)* | Limit which of the server's voices are exposed as engines, by program and voice name (see below) |
| Voice languages | *(empty)* | Limit the exposed engines to voices that speak one of these languages (see below) |
| Output format | `mp3` | The format rendered clips are stored and served in: `mp3` (small, ffmpeg-encoded), `wav` (plain PCM in a RIFF container) or `pcm` (the server's unmodified s16le output) |
| Synthesis timeout | `30` | Seconds to wait for a render before failing it (5–30). A render that takes that long is stuck, not slow: the timeout bounds how long an announcement can wait on a wedged server |

Raw PCM is an escape hatch for players or endpoints that can consume headerless PCM:
the bytes arrive exactly as the server sent them, with the sample rate advertised in
the `audio/L16;rate=…;channels=1` content type (and in `StreamDetails.audio_format`).
Most players cannot decode a headerless stream and should be left on MP3.

### The voice filters

Servers like wyoming-piper or Kokoro expose dozens of voices, in many languages; the
filters limit which of them become engines. Empty exposes all of them. Both filters
apply at once: a voice must pass whichever of them are set.

**Voice filter** (one matcher per value):

- A value with a colon is a program pattern and a voice pattern, both glob patterns:
  `piper:en_US-*`, `piper:en_US-lessac-medium`.
- A value equal to a program name selects all of its voices: `piper`.
- Any other value is a voice name pattern across all programs: `nl_NL-*`.
- `*` selects everything.
- Patterns are globs: `bf` matches only a voice named exactly `bf`; use `*bf*` to
  match names containing `bf`.

**Voice languages filter** (one pattern per value): a select with the languages the
server advertises; each value is a glob pattern matched against the voice's language
codes, and a voice passes when any of its languages matches any of the patterns.
`en` matches the code `en` exactly; `en-*` (hand-edited into the config) also matches
variants such as `en_GB`.

The filters are applied live, without reloading the provider, and the "Available
voices" line on the options page lists what the saved filters expose, with each
voice's language. Music Assistant only sends values to the provider on save, so the
line is a snapshot of the last saved filters, not a preview of the values being typed.
A combination that matches nothing is saved but exposes no engine: the provider goes
into the error state, the "Available voices" line says so, and the reason is on the
provider's details — adjust the filters until it lists the voices you want. A feature
that already picked one of the filtered-out voices surfaces Music Assistant's standard
"TTS engine unavailable" alert until you re-pick.

Rendered clips are cached on the server for 24 hours and served from a route on the
streamserver, so repeated announcements do not re-render.

### What the provider does not do

- **stdio servers** — the provider connects to an existing server; it does not spawn
  and supervise a process. Point it at `wyoming-piper -i tcp://...` instead.
- **Voice updates while running** — voices added to a running server appear after the
  provider is reloaded (Settings → Plugins → Wyoming TTS → reload).
- **Non-mono or non-16-bit audio** — such servers are refused with a clear error.

## Development

The repository follows the
[community provider template](https://github.com/music-assistant/community-provider-template);
the provider is developed against a pinned git commit of the
[Music Assistant server](https://github.com/music-assistant/server) in the dev
dependencies of `pyproject.toml`.

With [uv](https://docs.astral.sh/uv/):

```sh
uv sync          # create the venv with the pinned server
uv run pytest    # boot a real dev-mode server with this repository as its provider folder
uv run ruff check --no-fix .
uv run ruff format --check .
uv run mypy
```

The tests run the provider against an in-memory mock of the Wyoming client
(`tests/mock_client.py`): discovery, the setup flow, rendering (MP3, WAV and PCM),
program selection, the voice filters and the failure paths — no socket, no server
process. MP3 tests need the `ffmpeg` binary on the machine.

To develop against a real server:

```sh
./scripts/run.sh                # dev-mode Music Assistant server with this repository
# in another terminal
python -m wyoming_piper ... -i tcp://127.0.0.1:10200
```

## Release

A release is a GitHub release on a `vX.Y.Z` tag (not a pre-release). The
[community catalog](https://github.com/music-assistant/community-catalog) checks the
release: the manifest, the requirements (package names with version specifiers only,
available as wheels), and the provider loading in the server image of each channel.
