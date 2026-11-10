# Changelog

All notable changes to Voice Unleashed are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The version is the `version` in `plugin.yaml`, which the Hermes plugin catalog
entry names next to its pinned commit.

## [0.3.2]

### Changed

- README: HTTPS is not required for a device on the network. The section
  "HTTPS for your device" has a part "Without HTTPS" on the browser
  exception (Chrome's `unsafely-treat-insecure-origin-as-secure` flag), the
  listen address it needs and what it costs in security.

## [0.3.1]

### Added

- `examples/SOUL.md`: a recommended `SOUL.md` section for a profile you talk
  to, with rules for replies that are spoken as they stream. The README says
  where to put it.

### Changed

- README and the plugin description: the client is any device with a browser,
  a microphone and a speaker, not only a phone. The section "HTTPS for the
  phone" is now "HTTPS for your device". No change in behaviour.
- README: a "Full example" section with a complete `.env` and `config.yaml`.
  The `tool_progress` setting is written as `"off"` in quotes; a bare `off`
  is a boolean in YAML, which Hermes also takes.

## [0.3.0]

### Changed

- The plugin is renamed from VoiceManager Live to Voice Unleashed. It is a
  voice interface to Hermes, not only speech in and speech out, and the name
  now says so. VoiceManager is still the server it needs. Nothing else in its
  behaviour changes.
- In Hermes Desktop and the Webapp the card and its settings page are named
  "Voice Unleashed". They were named "Voice", which a search for the plugin
  name did not find.
- README: written new. It starts with what the interface does, gives one
  setup path, and has a section for voice in Hermes Desktop and the Webapp.

To upgrade from 0.2.1 or earlier: the old names are not read any more. Change
each one, then restart the gateway:

| Where | Old | New |
|---|---|---|
| Plugin name (`hermes plugins`, the `plugins.enabled` list, the plugin folder) | `voicemanager-live` | `voice-unleashed` |
| Platform id in `config.yaml` (`gateway.platforms`, `platforms`, `display.platforms`, `home_channel.platform`) | `voicemanager_live` | `voice_unleashed` |
| `.env` | `VMLIVE_TOKEN` | `VOICE_UNLEASHED_TOKEN` |
| `.env` | `VMLIVE_VOICEMANAGER_URL` | `VOICE_UNLEASHED_VOICEMANAGER_URL` |
| `.env` | `VMLIVE_VOICEMANAGER_TOKEN` | `VOICE_UNLEASHED_VOICEMANAGER_TOKEN` |
| `.env` | `VMLIVE_HOST` | `VOICE_UNLEASHED_HOST` |
| `.env` | `VMLIVE_PORT` | `VOICE_UNLEASHED_PORT` |
| `.env` | `VMLIVE_ALLOWED_USERS` | `VOICE_UNLEASHED_ALLOWED_USERS` |
| Repository | `TacoTakumi/hermes-voicemanager-live` | `TacoTakumi/hermes-voice-unleashed` |

The `extra` keys in `config.yaml` (`token`, `voicemanager_url`,
`voicemanager_token`, `host`, `port`) keep their names. Sessions held under
the old platform id stay in the Hermes state, but the page's own lane starts a
new conversation; pick an old one from the session list to go on with it.
The Desktop half has a new plugin id, so turn its switch on again and enter
its server address and token once more.

### Fixed

- In a Desktop or Webapp voice chat, what you say shows in the chat before
  Hermes starts to think. It showed only when the reply was complete. The
  Desktop half now has the chat read the turn again a moment after Hermes
  takes the utterance. Words you say over a running reply still show with
  that reply.
- `plugin.yaml` declares every hook the plugin registers. `on_stream_end`,
  `pre_llm_call`, `pre_tool_call` and `post_tool_call` were missing, which
  fails the catalog's check that declared capabilities match the code.
- README: what the `pre_llm_call` hook adds to a turn, and when.

## [0.2.1]

### Changed

- The page's default lane, the chat id Hermes sees when the page URL names no
  `client`, is `voice` instead of `phone`. A conversation held under the old
  lane is still reachable with `?client=phone`.
- README: how to set the `voice` lane as the platform's home channel, which
  stops Hermes asking for one at the start of every new voice session.

### Fixed

- Hermes no longer makes a TTS call of its own for a voice reply when
  `voice.auto_tts` is on or `/voice on` was given. VoiceManager speaks every
  reply, and the plugin dropped the extra audio after it was synthesized.
- A spoken "yes" or "no" resumes the right reply when Hermes steers or queues
  what you say during a blocked turn (`busy_input_mode` `steer` or `queue`):
  the answer after the approval is spoken again instead of being dropped.
- "New session" and "Session ended" no longer start with the last word of
  the reply they cut off.
- An approval prompt spoken on a page that reconnected during a blocked turn
  no longer leaves a TTS request open on VoiceManager.
- A failure of the approval check no longer ends the voice session; the word
  goes to Hermes as an ordinary message.
- An approval prompt for an empty command is spoken as "Approval needed. Say
  yes or no."
- A Desktop or Webapp chat that turns voice on while the voice page is still
  connecting to the same session keeps voice. Before, both ended up without it.
- A status line from Hermes before the answer starts, a provider fallback
  notice for example, no longer ends the turn on the page: it stays on
  Thinking and keeps its tool lines open until the turn is over. The server
  marks such a `reply` frame with `turn_running`.
- An answer is no longer shown twice when its last text arrives after the
  final message and ends in whitespace, or when
  `display.interim_assistant_messages` is off and the answer follows a tool
  call.
- A question the model asks before it goes on (the `clarify` tool) is spoken,
  with its choices and their numbers. Before, it was screen text only. The
  reply after your spoken answer is spoken too; it was dropped. A spoken
  number ("four", "number four", "the fourth one") or a spoken choice name
  selects that choice; anything else is a free answer.
- The spoken "Session ended" is no longer cut at its end on a phone. The page
  left as soon as its audio clock passed the end of the notice; the sound
  reaches the speaker later. It now waits until the notice has been heard,
  and it releases the microphone only then: a release while the notice
  played garbled its end. Until then the microphone is switched off inside
  the page.
- The spoken notices are "Session ended." and "New session." with a period,
  so that VoiceManager 0.8.3 speaks them as finished sentences. It adds a
  comma to a text that ends on a letter.
- When VoiceManager sends no audio for a reply (its speech engine did not
  load, for example), the page adds a note under the answer: "This was not
  spoken: the voice server sent no audio." Before, the answer was text with
  no sign that speech failed. The server sends the page a `speech_failed`
  frame and logs a warning.

## [0.2.0]

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
- A session chooser. Without `session` in its URL the voice page opens on a
  list: "New session" first, then the profile's sessions from any Hermes
  platform (Telegram, the CLI, Desktop...), the most recently active first, at
  most 20, a compacted conversation as one row. A row shows the title, else
  the first message, else the time, with how long ago it was active, its line
  count and its platform; a session with voice on in Hermes Desktop or the
  Webapp says "Voice on Desktop", and no id is shown. A tap on a row connects
  with `session=<id>`: the plugin sends `/resume <id> --all` on the
  conversation's behalf and keeps the gateway's text reply off the page. The
  owner is seeded as the platform's slash admin (`allow_admin_from: [owner]`,
  unless the setting is given) so the cross-session resume passes the
  gateway's guard without configuration. A tap on New connects with
  `session=new`, which sends `/new` only when the current session holds
  messages; an empty session is kept, and a conversation with no session yet
  gets one from its first utterance. `&session=<id>` in the page URL skips
  the chooser.
- The URL carries the session. A tap on a row puts `session=<id>` into the
  URL with a history entry; the chooser is the bare URL with the token only.
  Back returns from a session to the chooser with the microphone off, and
  Forward joins again. A load with `session=<id>` joins that session by
  itself and opens the microphone without a tap; a browser that keeps
  playback locked until a tap (Safari on iOS) shows a "Tap to unmute" button
  until it is tapped. The URL follows the session id of each history frame,
  so a reload after a spoken "new session" rejoins the new session.
- One voice device per profile. A second page takes the voice: the first
  page gets a `replaced` frame and close code 4001, stops its microphone,
  releases the wake lock and shows "Voice moved to another device" with a
  "Sessions" button that returns to the chooser. The hand-over also works
  with a UI client that has voice attached (Hermes Desktop, the Webapp): a
  page that joins its session sends that client `replaced` and 4001, so its
  voice toggle goes off and the chat stays, and a client that attaches voice
  to the session a page is on sends the page `replaced` and 4001, so it shows
  the moved screen.
- Every connect gets a `history` frame before `ready`: the session id and the
  last 40 stored user and assistant lines with text, oldest first, as
  `{"role": "user" | "hermes", "text": ...}`. The page shows them as turns and
  starts its log over on each history frame.
- Two spoken commands on the page, matched on the whole utterance after
  normalization (lower case, punctuation stripped, spaces collapsed), never on
  an attached session. "End session", "end the session" or "end this session"
  stops a playing reply and speaks "Session ended"; the server waits for the
  notice's audio to end (10 s at most), then sends a `session_end` frame and
  closes the connection. The page stops the microphone at once, lets the
  queued notice play out under the heading "Session ended", then releases the
  wake lock and shows the chooser with a fresh list. "New session", "start a
  new session" or "start new session" stops a playing reply, speaks "New
  session", applies the empty-session rule and runs `/new` for the page's
  conversation on the server, then sends a `session_new` frame with
  `session_id` (the fresh session's id) and a `history` frame with empty
  lines on the same connection; the page clears its log and listens. Nothing
  reconnects. When the gateway asks to confirm `/new` (the default
  `approvals.destructive_slash_confirm`), the plugin confirms its own
  command at once: tapping New or saying it was the confirmation.
- For other clients: `GET /sessions?token=...` (401 without the token,
  `Cache-Control: no-store`) answers a JSON array of rows with `id`, `title`,
  `preview`, `last_active` (epoch seconds), `message_count`, `source`,
  `voice_attached` (true when a UI client has voice attached to the session)
  and `current`. A session that cannot be joined gets a `join_failed` frame
  with a `message` and close code 4002, next to 4001 for a replaced voice
  device.

### Changed

- The voice page no longer sends the `client` URL parameter, so `&client=<name>`
  in a page URL does nothing: there is one voice device per profile, and a
  second page always takes the voice over. The replaced page's "In use on
  another device" screen with its "Use here" takeback is gone; it shows
  "Voice moved to another device" with a "Sessions" button that returns to
  the chooser instead.

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
- A page that connected while another page took over its chat id could move
  the shared conversation under the live page: its /resume or /new landed
  late, and the live page kept showing the old session. The replaced connect
  is now checked right after its join or new step, and when that step moved
  the lane the live page gets a fresh history frame.
- A /new that left the lane on the same session was presented as a new one:
  the page got session_new with the old id and the old history. The page now
  gets no session_new; it is told in a reply, and hears, that the new session
  could not be started.
- When the plugin cleared an attached UI client's reply because the user
  talked over its tail, the reply stayed open on the plugin until a new
  utterance or key replaced it. It is now closed on the plugin as well.
- A page replaced by another page, and a dropped VoiceManager connection, were
  logged at warning with a traceback, the drop four times per session. Each is
  now one line at info; other failures keep the warning and the traceback.
- Under multiplexed profiles, two profiles serving the page with the same
  client id got each other's reply text, and one profile's replies were
  silent. Stream hook events are now routed by the turn's profile as well as
  the chat id.
- On a session taken over from a Desktop voice chat, the page spoke the
  `<say>` block and the rest of the reply, and showed the tags. A reply that
  starts with a say block now speaks only that block; the tags are stripped
  from the shown text, the final reply and the history.
- A malformed `session=` value in the WebSocket URL (empty, longer than 256
  characters, or with a character outside letters, digits, underscore and
  dash) reached the gateway as the text of /resume. It is now refused with an
  error frame and close code 1008, as `attach=` is; an attach id with such a
  character is refused too.
- A profile with no state DB yet logged a warning with a traceback on every
  connect, and the session list answered 500. It now reads as empty. The
  message count before /new runs off the event loop.
- In classic end-of-turn mode, words heard between the end of a reply and the
  next turn_start acknowledgement were dispatched together with the question
  just answered. The answered segments are now cleared before the wait.
- The audio_start of a TTS request cancelled before its audio began was still
  forwarded, so the page adopted a dead request as its current reply. It is
  now dropped.
- The reply to a command the plugin sent (/resume, /new) was taken from
  whatever text arrived next, so a model reply that finished during a reload
  mid-turn was mistaken for the command's reply, and the command's reply then
  showed on the page. The command's reply is now matched by its reply_to, the
  command's own message id.
- On a window 900 px or wider but short (a small laptop window with the
  reasoning text open), the header column grew past the window and the whole
  page scrolled. The header now stops at the window height at every width:
  the reasoning text and the tool list shrink and scroll inside themselves
  first, and the header scrolls only when the rest of it does not fit.
- On a phone held sideways, where the header scrolls itself, a new tool line,
  a rotation or a transcript that wrapped could leave the newest tool line
  below the header's edge. A header left at its end now follows the newest
  tool line; a header you scrolled up stays where you left it until you
  scroll it back to its end.
- A tool call whose end never came (its reply was cut off) folded into the
  turn's summary line still marked running. It is now marked stopped when the
  turn ends.
- A flick of the tool list that kept scrolling for more than a second was not
  followed: a list flung back to its end could stay unpinned, and one flung
  away from it could jump when the list changed size. The scroll after a
  touch now counts until it stops.
- The meter's level array was named `history` and shadowed the browser's
  history object in the page's scope. It is renamed.
- Back or Forward to another session while the end-of-session notice was
  still playing joined it, then the end's wait tore the new session down
  once the notice would have ended. The wait is cancelled on a stop.
- A reply cut while it played reported `reply_sample` as the time since its
  first chunk, so a gap between chunks (a quiet stretch while tools ran, a
  slow network) counted as played audio. The voice page and the Desktop half
  now count the samples of the chunks that ended plus the elapsed part of
  the one playing.
- The spoken approval prompt read a script aloud when the command had
  several lines. It now speaks the first line and the count of the others:
  "Approval needed to run: python3 -, and 13 more lines. Say yes or no."
  The page still shows the full command.
- A start of the voice page that failed after the microphone was granted
  (the audio worklet did not load) left the microphone on and its audio
  context open. Both are released, and the capture context is resumed once
  made.
- A gateway reply that came with no turn (a busy notice, a refusal before
  the agent ran) left the voice page on "Thinking" until the next utterance.
  A reply frame for a turn that streamed no text now ends the turn on the
  page.
- The reason the voice session stopped was overwritten at once by the
  "Reconnecting" screen. The screen now repeats it until the next connection
  is ready.
- When the gateway's final reply text reached the voice page before the last
  streamed words, the answer showed twice and the late words started a third
  line. The final text now replaces the streamed line and the late words it
  already holds add nothing.
- The Desktop half counted microphone frames that its socket dropped while
  closed into `mic_sample`. It now counts only the frames that went out.
- The voice page read the microphone's capture latency on every 20 ms frame.
  It reads it once when the microphone starts.

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
