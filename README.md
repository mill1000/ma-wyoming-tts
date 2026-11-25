# Wyoming TTS for Music Assistant

A [Music Assistant](https://music-assistant.io) community plugin that provides local, private text-to-speech (TTS) via the [Wyoming protocol](https://github.com/OHF-Voice/wyoming).

Easily interface with your existing Wyoming TTS servers like:
- [OHF-Voice/wyoming-piper](https://github.com/OHF-Voice/wyoming-piper)
- [nordwestt/kokoro-wyoming](https://github.com/nordwestt/kokoro-wyoming)


By default, every TTS program & voice the server advertises is made available to Music Assistant as a selectable TTS engine. 

See [filtering languages and voices](#filtering-languages-and-voices) to reduce the options made available to Music Assistant.

## Installation

Search the Music Assistant Marketplace for "Wyoming TTS". Click the "+ Add" button.

## Configuration

Enter the address of an existing Wyoming TTS server (e.g. `127.0.0.1:10200`). The plugin will verify the server is accessible and advertises at least 1 TTS program & voice.

See [OHF-Voice/wyoming-piper](https://github.com/OHF-Voice/wyoming-piper) for more information on setting up a Wyoming TTS server.

### Options

| Option | Default | Meaning |
| --- | --- | --- |
| Voice filter | *(empty)* | Limits which programs and voices are exposed. See below. |
| Language filter | *(empty)* | Limits which languages are exposed. See below. |
| Output format | `wav` | Audio format to store and serve synthesized clips in. |
| Synthesis timeout | `30` | Seconds to wait for the server to synthesize a message, before the synthesis fails (5–30). |

## Filtering Languages And Voices

A server may advertise dozens of voices, in many languages. Filters limit which of these become TTS engines in Music Assistant. Leave them blank to expose all languages and voices.

### Voice Filters
The voice filters are a list of wildcard patterns. A voice is exposed when any of the filters match. 

A pattern is matched against the program and voice name, the program name, or the voice name:
* `piper:en*` - Expose all `en` voices provided by the `piper` program.
* `piper` - Expose all voices provided by the `piper` program.
* `bf*` - Expose all British female voices (from Kokoro).

### Language Filters
The language filter picks from the languages advertised by the server.
