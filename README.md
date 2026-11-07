# VoiceManager Live

Talk to Hermes hands-free from your phone, with your own speech stack.

Open a page on any phone or tablet, and Hermes listens while the page is open.
You talk, Hermes answers out loud, and you can talk over the answer to stop it.
Speech recognition, speech synthesis and turn-taking run on your own
[VoiceManager](https://github.com/TacoTakumi/VoiceManager) server. Any model
Hermes can use works, local or hosted.

## How it works

The plugin adds a gateway platform, `voicemanager_live`. It serves one small web
page and a WebSocket. For each open page:

- The page streams its microphone (16 kHz mono) to the plugin, and the plugin
  forwards it to VoiceManager on a full-duplex connection.
- VoiceManager decides when you stopped talking. With its turn engine (Tyger) on,
  it also hears the reply as it plays and tells when you cut in. Without the turn
  engine, the plugin ends a turn on VoiceManager's endpoint frames.
- Each finished utterance goes to Hermes as a voice message in that page's
  conversation. Tools and memory work as on any other platform. Approvals do not:
  see "Things to know".
- The reply's text goes to VoiceManager while the model writes it, so speech
  starts before the reply is complete. The page plays the audio and shows the
  conversation as text.
- When you talk over a reply, the audio stops at once and the rest of that reply
  is dropped. Your new words then go to Hermes, which stops the old turn.

## What you need

- Hermes Agent 0.23.0 or later.
- A VoiceManager server (0.7.0 or later) that your Hermes gateway can reach. See
  its README for the engines and the GPU it needs. For barge-in on a phone
  speaker, run it with `VM_STT_ENGINE=audiocpp` and `VM_TURN_ENGINE=tyger`.
  `VM_TYGER_PREFILTER=voxlock` helps when the phone plays the reply on its
  loudspeaker.
- HTTPS in front of the page when the phone is not the machine Hermes runs on.
  Browsers allow the microphone only on `https://` pages or on `localhost`.

## Install

From the Hermes plugin catalog:

```sh
hermes plugins install voicemanager-live
```

Or from a checkout, for development:

```sh
git clone https://github.com/TacoTakumi/hermes-voicemanager-live ~/.hermes/plugins/voicemanager-live
hermes plugins enable voicemanager-live
```

## Set up VoiceManager

Create a token for Hermes on the VoiceManager host:

```sh
voicemanager user token add you hermes-voice-live
```

It prints the token once. It starts with `vmp_`.

## Configure

Set these in the Hermes profile's `.env` (or with `hermes config`):

| Variable | Required | Meaning |
|---|---|---|
| `VMLIVE_TOKEN` | yes | Access token for the page. Pick a long random string. |
| `VMLIVE_VOICEMANAGER_URL` | yes | VoiceManager URL, for example `http://127.0.0.1:8255`. |
| `VMLIVE_VOICEMANAGER_TOKEN` | when VoiceManager asks for one | The `vmp_` token from above. |
| `VMLIVE_HOST` | no | Address the page listens on. Default `127.0.0.1`. |
| `VMLIVE_PORT` | no | Port the page listens on. Default `8765`. |
| `VMLIVE_ALLOWED_USERS` | yes | Set it to `owner`. Whoever holds the access token is the one user of this platform. |

The same settings can go in `config.yaml` under
`gateway.platforms.voicemanager_live.extra` as `token`, `voicemanager_url`,
`voicemanager_token`, `host` and `port`. The environment wins.

Restart the gateway. Its log says `voicemanager_live connected` and
`voice page listening on <host>:<port>`.

### Thinking models

Voice turns use Hermes's `auxiliary.voice_chat` settings. By default they turn
reasoning off, because a model that thinks first leaves you in silence until the
first word. To keep your profile's own reasoning for voice, set:

```yaml
auxiliary:
  voice_chat:
    reasoning_effort: ""
```

`auxiliary.voice_chat.model` and `provider` can also send voice turns to a
faster model than the profile's main one.

## Open the page

```
https://<your-host>/?token=<VMLIVE_TOKEN>
```

Tap "Start talking" and allow the microphone. The page keeps the screen on.
Add `&client=<name>` to give a device its own conversation; the default is
`phone`. Add `&aec=0` to turn off the browser's echo cancellation and noise
suppression.

## HTTPS for the phone

Keep `VMLIVE_HOST=127.0.0.1` and put a TLS proxy in front of the port. Two ways
that work:

- Tailscale: `tailscale serve --bg 8765`, then open
  `https://<machine>.<tailnet>.ts.net/?token=...` on the phone.
- Caddy:

  ```
  voice.example.lan {
      reverse_proxy 127.0.0.1:8765
  }
  ```

The proxy must pass WebSocket upgrades on `/ws`. Both of these do.

## Things to know

- The plugin registers Hermes hooks (`on_stream_delta`, `on_session_end`) to
  read reply text as it is written. A registered stream
  hook makes Hermes use streaming requests for every conversation, not only
  voice ones. Replies on other platforms do not change.
- The page and its WebSocket check the access token on every request. The token
  is in the page URL, so treat the URL like a password.
- Approvals cannot be given by voice. When a command needs your approval, the
  prompt shows as text on the page and is not spoken. Saying "yes" does not
  approve it: your words reach Hermes as a new message, which stops the waiting
  turn, and the command does not run. Use the voice page for conversation, and
  run commands that need approval from another platform or the CLI.
- The first question after a gateway start is slow: 14 to 16 s on our test rig,
  against about 1.5 s for the questions after it. The agent and the model's prompt
  cache start cold. Ask one question after a restart before you rely on it.
- The page listens on the address you set and serves only the page and its
  WebSocket. It stores nothing.
- The plugin makes no network calls except to your VoiceManager server.
- `vm_client.py` is VoiceManager's standalone Python client from v0.7.0,
  copied in because VoiceManager is not on PyPI.

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
