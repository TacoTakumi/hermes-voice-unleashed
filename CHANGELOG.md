# Changelog

All notable changes to VoiceManager Live are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The version is the `version` in `plugin.yaml`, which the Hermes plugin catalog
entry names next to its pinned commit.

## [0.1.1]

### Added

- Voice for a chat in Hermes Desktop and the Webapp. The plugin folder now
  holds a desktop half, `desktop/plugin.js`, which Hermes copies into its
  desktop plugins on load. It ships switched off: turn on its Desktop switch in
  Capabilities > Plugins > Voice. It adds a voice toggle to the composer and a
  settings page (Settings > Plugins > Voice) for the plugin's WebSocket address
  and access token, kept in the app's plugin storage.
- With voice on, the microphone and the speaker are on the machine that runs
  the UI. The desktop half streams the microphone to the plugin, plays the reply
  audio and stops at once when you talk over it, as the voice page does.
- Each finished utterance goes into the open chat as your message. The reply
  prints as usual, and only its first `<say>...</say>` block is spoken. A reply
  without one is not spoken. Replies to typed messages in a voice chat are
  spoken the same way.
- A `pre_llm_call` hook asks the model for that `<say>` block, only for chats
  that have voice on.
- WebSocket attach mode: `/ws?token=...&attach=<session id>` binds a voice
  session to a Hermes chat instead of the voice page's own conversation. The
  client sends the reply's spoken text as `say` frames and ends it with
  `say_end`, each with a `reply` key; text for a reply that was cut off is
  dropped. An attach id longer than 256 characters is refused.

## [0.1.0] - 2026-11-07

First version.

### Added

- The `voicemanager_live` gateway platform. It serves one web page and its
  WebSocket on `VMLIVE_HOST:VMLIVE_PORT` (default `127.0.0.1:8765`). The page and
  the socket need the access token `VMLIVE_TOKEN` as the `token` query parameter;
  a request without it gets 401 before any handshake. Settings come from the
  environment or from `gateway.platforms.voicemanager_live.extra` in
  `config.yaml`; the environment wins.
- One VoiceManager full-duplex session per open page
  (`VMLIVE_VOICEMANAGER_URL`, bearer `VMLIVE_VOICEMANAGER_TOKEN`). The session
  asks for the Tyger turn engine before the first audio frame. When the server
  refuses it, or loses it later, the session ends a turn on VoiceManager's
  endpoint frames instead: it dispatches the first endpointed transcript, starts
  over when the dispatched words are revised, and treats new words during a
  reply as talking over it.
- Each finished utterance goes to Hermes as a voice message in the page's
  conversation, so it takes Hermes's voice-turn route
  (`auxiliary.voice_chat`): reasoning off by default, or the profile's own
  setting when `auxiliary.voice_chat.reasoning_effort` is an empty string. Each
  utterance carries its own message id, which keeps the text of an older reply
  out of a newer one.
- Replies are spoken while the model writes them. The plugin reads the reply
  text through the `on_stream_delta` and `on_session_end` hooks, keeps the
  events whose session platform is `voicemanager_live`, and finds the page by the
  session's chat id. Registering a stream hook makes Hermes stream every reply,
  so this needs no gateway streaming setting. Text goes to one clause-mode TTS
  request per reply in whole words, up to the last whitespace, and the request
  ends after the reply's text has been quiet for 0.3 s.
- Barge-in. When the turn engine hears the user cut in, or when new words
  arrive over a reply without it, the page drops its queued audio at once and
  the rest of that reply's text is not spoken. The user's next utterance then
  stops the Hermes turn, through the gateway's `busy_input_mode: interrupt`.
- One page owns a conversation, keyed by the `client` query parameter (default
  `phone`). A second page with the same id takes it over: the first page gets a
  `replaced` frame and close code 4001, stops reconnecting, and offers "Use
  here" to take the conversation back.
- The page: it streams the microphone as 16 kHz mono 16-bit PCM in 20 ms frames,
  plays each reply at its own sample rate, and reports the reply's playback start
  and stop to the turn engine. It shows a large state word (Listening, Hearing
  you, Thinking, Speaking), a level meter, the live transcript and the
  conversation, keeps the screen awake, and reconnects with backoff. `aec=0` in
  the URL turns off the browser's echo cancellation and noise suppression.
- A session whose VoiceManager reader fails or loses its connection logs the
  reason, shows it on the page and closes; the page reconnects.
- `play_tts` does nothing on this platform, so Hermes's own auto-TTS never
  speaks a reply a second time.
- VoiceManager's standalone Python client from v0.7.0, vendored as
  `vm_client.py`, because VoiceManager is not on PyPI.
- `scripts/test.sh` (unit and protocol tests against a Hermes checkout and a fake
  VoiceManager), `scripts/test-browser.sh` (the page in headless Chrome with a
  fake microphone) and `scripts/live_check.sh` (a throwaway gateway against a
  real VoiceManager and model, timing the first reply audio after the end of a
  turn).
- Requires Hermes 0.22.7 or later.
