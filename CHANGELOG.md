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
  prints as usual, and the first `<say>...</say>` block of each assistant
  message is spoken, so a turn with tool calls can speak more than once. A
  message without one is not spoken. Replies to typed messages in a voice chat are
  spoken the same way.
- A `pre_llm_call` hook asks the model for that `<say>` block, only for chats
  that have voice on.
- WebSocket attach mode: `/ws?token=...&attach=<session id>` binds a voice
  session to a Hermes chat instead of the voice page's own conversation. The
  client sends the reply's spoken text as `say` frames and ends it with
  `say_end`, each with a `reply` key; text for a reply that was cut off is
  dropped. Each `utterance` and `clear` frame to the client carries a rising
  `epoch`. The client puts the last epoch it got into each `say` and
  `say_end`, and the session drops frames of an older epoch. An attach id that
  is empty or longer than 256 characters is refused.
- The voice page shows "Working" while a Hermes turn still runs and no reply
  audio plays, for example between a spoken "let me check" and the answer. The
  server sends a `turn_done` frame to the page when the turn and its reply
  audio are over.
- An activity panel on the voice page shows each tool call of the turn: the
  tool, a short form of its arguments, and its state with its time: running,
  done, failed, timed out, blocked or cancelled. A call still running when the
  connection drops shows "stopped". The panel holds about six lines, follows
  the newest one unless you scroll it, and folds with "Hide tools". The server
  sends `tool_start` and `tool_end` frames from Hermes's tool hooks, and the
  gateway's own tool progress messages are off for this platform by default.
- A reasoning line on the voice page shows the newest sentence of the model's
  reasoning while it thinks, the whole text on a tap, and "Reasoned for m:ss"
  when the turn ends. It needs `plugins.stream_reasoning_deltas: true`; the
  server forwards the reasoning as `reasoning_delta` frames and never speaks it.
- While the page shows Thinking or Working, a line under it shows the time since
  you spoke and the number of tool calls so far. At the end of a turn its tool
  lines fold into one line, such as "3 tools, 0:42", that opens on a tap.
- The voice page and the Desktop half send `mic_sample` in their `playback`
  frames: where on the mic stream a reply's start and stop reached the
  speaker, read from the audio output clock and the microphone's capture
  latency. The plugin passes it to VoiceManager and falls back to its own
  count of forwarded mic samples when a client sends none. The README says
  how another client of `/ws` computes it.
- Approvals by voice. When a command needs approval, the page shows the
  gateway's prompt and speaks "Approval needed to run: <command>. Say yes or
  no." A one-word answer reaches Hermes without the speech recognizer's
  punctuation, so "Yes." approves and "No." denies, and the turn that resumes
  is spoken: the plugin keeps the waiting reply open under its id instead of
  opening a new one for the answer. Without a connected page the gateway sends
  its text prompt as before.

### Fixed

- The voice page showed "Speaking" and "Talk over it to stop it." after the
  reply audio stopped while the turn still ran, so speaking then stopped the
  turn. It now shows "Speaking", and moves the amber meter, only while reply
  audio plays.
- A reply that went quiet for longer than VoiceManager's TTS flush timeout (10
  s by default) while Hermes ran tools lost the rest of its audio, and the
  voice page stayed on "Working" after the turn. The rest of the reply now goes
  into a new TTS request, and the turn ends as usual.
- The last word of text the model wrote just before a tool call ("Standing
  by." spoken as "Standing") waited until the turn ended, so it came after the
  tools ran, or never when you cut in. It is now spoken when the model's
  response ends.
- `plugin.yaml` gave port 8250 in its VoiceManager URL example; the default is
  8255. It also listed `VMLIVE_ALLOWED_USERS` as optional, but no utterance is
  accepted without it, so it is now required.
- An empty `attach=` in the WebSocket URL took over the voice page's own
  conversation. It is now refused.
- The folded reasoning line showed the start of a long run when the model
  reasoned in lists or paragraphs with no sentence punctuation. It now also
  splits on line breaks.
- A reply's playback edges were placed early on the mic timeline: the page
  reported the start when its clock reached the scheduled time, before the
  sound left the speaker, and the plugin stamped the edge with the mic samples
  it had forwarded so far, behind the capture. On a phone or a Bluetooth
  headset VoiceManager's echo hold and agent channel missed part of the reply,
  so echo could reach the speech recognizer or a barge-in could be missed.

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
