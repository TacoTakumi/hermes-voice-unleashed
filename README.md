# Voice Unleashed

A voice interface to Hermes. Talk to your agent hands-free, hear it answer,
and see what it does while it works.

Most voice add-ons turn speech into text and text into speech. Voice Unleashed
makes voice the way you run the agent:

- **Talk and listen, full duplex.** Open a page on any device such as a
  phone or tablet, and Hermes listens while the page is open. It answers out
  loud while the model still writes, and you talk over the answer to stop it.
- **Any device.** The client is a web page. Anything with a browser, a
  microphone and a speaker can be the voice of your agent: a phone in your
  pocket with one earpiece, a tablet on the kitchen wall, a spare laptop, a
  display in the workshop.
- **See the work.** The page shows the conversation, each tool call as it
  runs and the model's reasoning, so a silent agent is never a mystery.
- **Decide by voice.** Approval prompts and the model's questions are spoken.
  You answer "yes", "no", a number or your own words.
- **Pick up any session.** The page lists the profile's sessions from every
  platform (Telegram, the CLI, Desktop). Go on with one or start a new one.
  "New session" and "end session" also work as spoken commands.
- **Voice in Hermes Desktop and the Webapp.** A chat there gets a voice
  toggle: you speak to the chat and hear a short spoken summary of each reply.
  The voice moves between your devices.
- **Your own speech stack.** Speech recognition, speech synthesis and
  turn-taking run on your own
  [VoiceManager](https://github.com/TacoTakumi/VoiceManager) server. Any model
  Hermes can use works, local or hosted.

## What you need

- Hermes Agent 0.22.7 or later.
- A VoiceManager server (0.7.0 or later) that your Hermes gateway can reach.
  See its README for the engines and the GPU it needs. For barge-in on a
  device that plays the reply on its own loudspeaker, run it with
  `VM_STT_ENGINE=audiocpp` and `VM_TURN_ENGINE=tyger`.
  `VM_TYGER_PREFILTER=voxlock` helps against the echo of that loudspeaker.
- A page the browser treats as secure, when the device is not the machine
  Hermes runs on. Browsers allow the microphone only on `https://` pages, on
  `localhost`, or on an address you mark as secure in the browser. So put
  HTTPS in front of the page, or add a browser exception; see "HTTPS for
  your device".

## Install

From the Hermes plugin catalog:

```sh
hermes plugins install voice-unleashed
```

Before the catalog lists it, install it straight from GitHub:

```sh
hermes plugins install TacoTakumi/hermes-voice-unleashed
```

Or from a checkout, for development:

```sh
git clone https://github.com/TacoTakumi/hermes-voice-unleashed ~/.hermes/plugins/voice-unleashed
hermes plugins enable voice-unleashed
```

## Set up VoiceManager

Create a token for Hermes on the VoiceManager host:

```sh
voicemanager user token add you hermes-voice-unleashed
```

It prints the token once. It starts with `vmp_`.

## Configure

Set these in the Hermes profile's `.env` (or with `hermes config`):

| Variable | Required | Meaning |
|---|---|---|
| `VOICE_UNLEASHED_TOKEN` | yes | Access token for the page. Pick a long random string. |
| `VOICE_UNLEASHED_VOICEMANAGER_URL` | yes | VoiceManager URL, for example `http://127.0.0.1:8255`. |
| `VOICE_UNLEASHED_ALLOWED_USERS` | yes | Set it to `owner`. Whoever holds the access token is the one user of this platform. |
| `VOICE_UNLEASHED_VOICEMANAGER_TOKEN` | when VoiceManager asks for one | The `vmp_` token from above. |
| `VOICE_UNLEASHED_HOST` | no | Address the page listens on. Default `127.0.0.1`. |
| `VOICE_UNLEASHED_PORT` | no | Port the page listens on. Default `8765`. |

The same settings can go in `config.yaml` under
`gateway.platforms.voice_unleashed.extra` as `token`, `voicemanager_url`,
`voicemanager_token`, `host` and `port`. The environment wins.

Restart the gateway. Its log says `voice_unleashed connected` and
`voice page listening on <host>:<port>`.

## Full example

A complete setup in one place. Only the three required variables are needed;
everything else is optional and explained in the sections below.

The profile's `.env`:

```sh
# Required
VOICE_UNLEASHED_TOKEN=pick-a-long-random-string
VOICE_UNLEASHED_VOICEMANAGER_URL=http://127.0.0.1:8255
VOICE_UNLEASHED_ALLOWED_USERS=owner

# When VoiceManager asks for a token
VOICE_UNLEASHED_VOICEMANAGER_TOKEN=vmp_your-token

# Where the page listens (these are the defaults)
VOICE_UNLEASHED_HOST=127.0.0.1
VOICE_UNLEASHED_PORT=8765
```

The profile's `config.yaml`:

```yaml
plugins:
  enabled:
    - voice-unleashed
  # Lets the page show the model's reasoning line.
  stream_reasoning_deltas: true

# The plugin's own settings, if you prefer config.yaml to .env. A variable
# in .env wins over the same setting here. VOICE_UNLEASHED_ALLOWED_USERS has
# no key here and stays in .env.
gateway:
  platforms:
    voice_unleashed:
      extra:
        token: pick-a-long-random-string
        voicemanager_url: http://127.0.0.1:8255
        voicemanager_token: vmp_your-token
        host: 127.0.0.1
        port: 8765

platforms:
  voice_unleashed:
    # Stops Hermes asking for a home channel in every new voice session.
    home_channel:
      platform: voice_unleashed
      chat_id: voice
      name: Voice

display:
  platforms:
    voice_unleashed:
      # The page shows tool calls and reasoning itself.
      tool_progress: "off"
      show_reasoning: false

auxiliary:
  voice_chat:
    # Keeps the profile's own reasoning for voice turns. Leave this key out
    # to turn reasoning off for a faster first word.
    reasoning_effort: ""
```

Voice in Hermes Desktop and the Webapp has no file to edit: its server
address and access token are entered in the app (see below).

## HTTPS for your device

Keep `VOICE_UNLEASHED_HOST=127.0.0.1` and put a TLS proxy in front of the
port. Two ways that work:

- Tailscale: `tailscale serve --bg 8765`, then open
  `https://<machine>.<tailnet>.ts.net/?token=...` on the device.
- Caddy:

  ```
  voice.example.lan {
      reverse_proxy 127.0.0.1:8765
  }
  ```

The proxy must pass WebSocket upgrades on `/ws`. Both of these do.

### Without HTTPS

HTTPS is not required. A browser can be told to treat one plain `http://`
address as secure, and then it gives that page the microphone:

1. Make the page reachable from the device: set `VOICE_UNLEASHED_HOST=0.0.0.0`
   (or the machine's LAN address) and restart the gateway.
2. In Chrome on the device, desktop or Android, open
   `chrome://flags/#unsafely-treat-insecure-origin-as-secure`, enter the
   page's exact origin with scheme, host and port, for example
   `http://192.168.1.20:8765`, enable the flag and relaunch the browser.
3. Open `http://192.168.1.20:8765/?token=...`.

Other browsers have their own setting for this, and some have none; use
HTTPS there. Without HTTPS the access token and the audio cross the network
unencrypted, so do this only on a network you trust.

## Open the page

```
https://<your-host>/?token=<VOICE_UNLEASHED_TOKEN>
```

With a browser exception instead of HTTPS, the address is
`http://<host>:8765/?token=<VOICE_UNLEASHED_TOKEN>`.

Tap "New session" or a session in the list, and allow the microphone. Then
talk. The page keeps the screen on.

The first question after a gateway start is slow: 14 to 16 s on our test rig,
against about 1.5 s for the questions after it. The agent and the model's
prompt cache start cold. Ask one question after a restart before you rely on
it.

## Using the page

### Sessions

The page opens on a list of sessions: a "New session" button first, then the
profile's sessions, the most recently active first, at most 20. A session can
come from any Hermes platform (Telegram, the CLI, Desktop...), so the page
can pick up a conversation you started elsewhere. A conversation that Hermes
has compacted is one row. Each row shows the session's title, else its first
message, else its time, with how long ago it was active, its number of lines
and the platform it came from. A row whose session has voice on in Hermes
Desktop or the Webapp says "Voice on Desktop". Row ids are not shown.

Tap a row to go on with that session, or "New session" for a fresh one. The
last 40 lines of the session appear in the log. "New session" opens a fresh
session only when the one the page is on holds messages; an empty session is
used as it is, so a second tap on New does not stack up empty sessions.

The URL follows the page. A tap on a row puts `&session=<id>` into it; the
chooser is the bare URL with the token only. The browser's Back button
returns from a session to the chooser with the microphone off, and Forward
joins the session again. A reload while on a session joins it again by itself
and opens the microphone without a tap, also after a spoken "new session",
because the URL takes the id of the session the page lands on. A browser that
keeps playback locked until a tap (Safari on iOS) shows a "Tap to unmute"
button until you tap it.

Add `&session=<Hermes session id>` to the URL to skip the list and join that
session at once; `&session=new` skips it and starts a fresh one, under the
same rule. Add `&aec=0` to turn off the browser's echo cancellation and noise
suppression. Add `&client=<name>` to talk on another lane than `voice` (see
"Home channel").

### One voice device

There is one voice device per profile. When you open the page on a second
device or tab, that page takes the voice: the first one shows "Voice moved to
another device" with a "Sessions" button that returns to the chooser; its
microphone is off and the screen may go to sleep. The same hand-over works
with Hermes Desktop and the Webapp. When the page joins a session that has
voice on there, the voice toggle on that client goes off and the chat stays;
when you turn voice on there for the session the page is on, the page shows
the moved screen.

### What you see while Hermes works

- The conversation as text, your words and the replies.
- An activity panel with each tool call of the turn, as it starts and ends.
- A reasoning line under the transcript: the newest sentence while the model
  thinks, the whole text when you tap it, and "Reasoned for m:ss" when the
  turn ends. It needs a setting; see "Thinking models".
- A note under an answer when it could not be spoken: "This was not spoken:
  the voice server sent no audio."

### Talking over a reply

When you talk over a reply, the audio stops at once and the rest of that reply
is dropped. Your new words then go to Hermes, which stops the old turn.

### Spoken commands

Two spoken commands end or restart the session from the page. A command is
the whole utterance, in any case and with any punctuation:

- "end session", "end the session" or "end this session" stops a reply that
  is playing and says "Session ended". The page stops the microphone at once,
  lets the notice play out under the heading "Session ended", then lets the
  screen go off and shows the session list again.
- "new session", "start a new session" or "start new session" stops a playing
  reply, says "New session", moves the connection onto a fresh session and
  clears the page's log. The microphone stays on and nothing reconnects. An
  empty session is kept, as above.

Anything else ("end the session now") reaches Hermes as a message.

### Approvals

When a command needs your approval, the page shows the gateway's prompt and
speaks one sentence: "Approval needed to run: <the command>. Say yes or no."
An approval word said on its own ("yes", "approve", "ok", "no", "deny")
answers it exactly as typing it would on another platform; punctuation from
the speech recognizer ("Yes.") does not matter. The turn then goes on and its
reply is spoken as usual. Anything longer ("yes, go ahead") reaches Hermes as
a new message, which stops the waiting turn, and the command does not run.
The gateway's confirmation ("Allowed once") is screen text.

### Questions from the model

When Hermes asks you something before it goes on (its `clarify` tool), the
page shows the question and speaks it; a question with choices is read with
their numbers ("One: ... Two: ... Say the number or your answer."). The next
thing you say is the answer, and the turn then goes on and its reply is
spoken as usual. A number ("four", "number four", "the fourth one") or a
choice's name selects that choice; anything else reaches Hermes as your own
answer. "Skip" skips the question; "new session" and "end session" are still
commands.

## Voice in Hermes Desktop and the Webapp

The plugin folder holds a Desktop half, `desktop/plugin.js`, which Hermes
copies into its desktop plugins on load. It ships switched off.

1. Open Capabilities > Plugins, click FILTERS and then "Installed". The page
   lists the catalog first, so the card is hard to find without the filter.
2. Open the "Voice Unleashed" card and turn on its Desktop switch.
3. Open Settings > Plugins > Voice Unleashed and enter the voice server
   address (the address of the voice page, for example
   `ws://127.0.0.1:8765`, or the `https://` address of your proxy) and the
   access token (`VOICE_UNLEASHED_TOKEN`). They are kept in the app's plugin
   storage, for each browser.
4. Use the voice toggle in the composer of a chat.

With voice on, the microphone and the speaker are on the machine that runs
the UI. Each finished utterance goes into the open chat as your message, and
shows there before Hermes starts to think. The
reply prints as usual, and a short summary of it is spoken: the plugin asks
the model to start each reply with a `<say>...</say>` block, and that block
is what you hear. A message without one is not spoken. Replies to typed
messages in a voice chat are spoken the same way. You can talk over the
speech to stop it, as on the page.

The spoken commands of the page do nothing here: every utterance goes to the
chat.

## More configuration

### A SOUL.md for a voice profile

On the page the whole reply is spoken while the model writes it, so a model
that answers in markdown, long lists or file paths is hard to listen to.
[examples/SOUL.md](examples/SOUL.md) is a starting point for a profile you
talk to: lead with the answer, short sentences, nothing that only works on a
screen. Copy it to the profile's `SOUL.md` (`~/.hermes/SOUL.md`, or
`~/.hermes/profiles/<name>/SOUL.md`), or add its section to the one you
have, and put your own persona around it.

A profile you use only from Hermes Desktop or the Webapp needs it less:
there the reply prints in full and only its short `<say>` summary is spoken,
which the plugin asks for itself.

### Home channel

Hermes asks every platform for a home channel, the chat where it delivers cron
results and messages from other platforms. Until one is set it says so at the
start of every new voice session. The page talks on the lane `voice` unless
its URL names another with `?client=<name>`, so set that lane as the home
channel in the profile's `config.yaml`:

```yaml
platforms:
  voice_unleashed:
    home_channel:
      platform: voice_unleashed
      chat_id: voice
      name: Voice
```

A message to the home channel reaches the page only while it is open on that
lane. The plugin drops it otherwise.

### Thinking models

Voice turns use Hermes's `auxiliary.voice_chat` settings. By default they turn
reasoning off, because a model that thinks first leaves you in silence until
the first word. To keep your profile's own reasoning for voice, set:

```yaml
auxiliary:
  voice_chat:
    reasoning_effort: ""
```

`auxiliary.voice_chat.model` and `provider` can also send voice turns to a
faster model than the profile's main one.

The page's reasoning line needs two things: the voice turn must keep its
reasoning (above), and Hermes must pass reasoning to plugins, which it does
only when you turn it on:

```yaml
plugins:
  stream_reasoning_deltas: true
```

Without it the line stays hidden.

### Tool calls on the page

The page shows each tool call in its activity panel, so the plugin turns
Hermes's own tool progress messages off for this platform. A `tool_progress`
setting in your `config.yaml` wins over that, and `display.tool_progress: all`
puts every progress message in the conversation log as well. Turn it off for
this platform only. `display.show_reasoning: true` likewise puts a reasoning
block in front of every answer in the log, where the page already shows the
reasoning in its own line; turn that off for this platform too:

```yaml
display:
  platforms:
    voice_unleashed:
      tool_progress: "off"
      show_reasoning: false
```

## How it works

The plugin adds a gateway platform, `voice_unleashed`. It serves one small web
page and a WebSocket. For each open page:

- The page streams its microphone (16 kHz mono) to the plugin, and the plugin
  forwards it to VoiceManager on a full-duplex connection.
- VoiceManager decides when you stopped talking. With its turn engine (Tyger)
  on, it also hears the reply as it plays and tells when you cut in. Without
  the turn engine, the plugin ends a turn on VoiceManager's endpoint frames.
- Each finished utterance goes to Hermes as a voice message in that page's
  conversation. Tools, memory and approvals work as on any other platform.
- The reply's text goes to VoiceManager while the model writes it, so speech
  starts before the reply is complete. The page plays the audio and shows the
  conversation as text.

## What the plugin does on your system

- It listens on the address you set and serves only the page, its session
  list and its WebSocket. All three check the access token on every request.
  The token is in the page URL, so treat the URL like a password.
- It makes no network calls except to your VoiceManager server. Microphone
  audio and reply text go there and nowhere else.
- It stores nothing. The sessions and their history are read from the
  profile's own Hermes state.
- It registers stream hooks (`on_stream_delta`, `on_stream_end`,
  `on_session_end`) to read reply text as it is written. A registered stream
  hook makes Hermes use streaming requests for every conversation, not only
  voice ones. Replies on other platforms do not change.
- It registers `pre_tool_call` and `post_tool_call` to show tool calls on the
  page. Hermes treats `pre_tool_call` as a guard: when a plugin's callback
  raises, or Hermes cannot run it within `plugins.hook_callback_timeout`
  (30 s by default), it blocks that tool call. The hook runs for every
  conversation in the profile, not only voice ones. This plugin's callback
  catches its own errors, does no blocking work and only passes the event on,
  so it allows every call.
- It registers `pre_llm_call`. For a Desktop or Webapp chat that has voice
  on, and only then, the hook adds one instruction to the turn: start the
  reply with a short spoken summary inside `<say>...</say>`. No other
  conversation gets the instruction.
- `vm_client.py` is VoiceManager's standalone Python client from v0.7.0,
  copied in because VoiceManager is not on PyPI. The plugin has no Python
  dependencies beyond what Hermes ships.

## For your own client

The page and the Desktop half are clients of the plugin's HTTP and WebSocket
interface. You can write another.

- `GET /sessions?token=...` (401 without the token, sent with
  `Cache-Control: no-store`) answers the session list as a JSON array, most
  recently active first. Each row has `id`, `title` (null when the session
  has none), `preview` (its first user message, short), `last_active` (epoch
  seconds), `message_count`, `source` (the platform it came from),
  `voice_attached` (true when a UI client such as Hermes Desktop has voice
  attached to it) and `current` (whether the page's conversation is on it;
  the page ignores it).
- The page's conversation is keyed inside the plugin: a connect to `/ws`
  needs no parameter beyond the token, and a second connection replaces the
  first. `/ws?...&session=<id>` joins that session on connect, `session=new`
  asks for a fresh one under the rule in "Sessions", and without `session`
  the connection stays on the session the conversation is on.
- Every connect gets one `history` frame before `ready`: `{"type":
  "history", "session_id": ..., "lines": [{"role": "user" | "hermes", "text":
  ...}]}`, the last 40 stored user and assistant lines with text, oldest
  first; `session_id` is null and `lines` empty when the conversation has no
  session yet.
- When the session cannot be joined, the client gets a `join_failed` frame
  with a `message` and the socket closes with code 4002.
- A connection that another voice device replaced gets a `replaced` frame and
  close code 4001, and must not reconnect: a page gets it when a second page
  connects or when a UI client attaches voice (`attach=<session id>`) to the
  session it is on, and an attached UI client gets it when a page joins its
  session.
- The spoken commands send a `session_end` frame once the server's notice has
  played (10 s at most), after which it closes the socket, or a `session_new`
  frame with `session_id` (the id of the fresh session) followed by a
  `history` frame with empty lines on the same connection, which stays open
  and takes the next utterance. An attached client gets every utterance and
  these commands do nothing there.
- A client reports each reply's playback edges as `playback` frames with
  `event` `start` or `stop`, the `request_id`, and `mic_sample`: the number
  of 16 kHz mic samples the client had sent on that connection when the edge
  reached the speaker. Compute it from the device clocks: the speaker time of
  the edge minus the capture time of the last mic frame sent, times 16000,
  added to the count of samples sent. Count only the samples you send (none
  while muted), and never send a stop with a smaller `mic_sample` than its
  start. A frame without a valid `mic_sample` still works: the plugin uses
  its own count of the mic samples it forwarded, which places the edge early
  by the device latencies.

## Development

```sh
scripts/test.sh -q          # unit and protocol tests, against a Hermes checkout
scripts/test-browser.sh -q  # the page in headless Chrome
```

`scripts/test.sh` expects a hermes-agent checkout at `../hermes-agent` (or
`HERMES_CHECKOUT`) with its `.venv`. The browser test needs its own venv:

```sh
python3 -m venv .venv-test && .venv-test/bin/pip install -r tests/browser/requirements.txt
```

`scripts/live_check.sh` runs a throwaway gateway with this plugin against your
VoiceManager and model, asks one warm-up question, then times five spoken
questions from the end of the turn to the first reply audio.

## License

MIT
