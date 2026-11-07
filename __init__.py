"""VoiceManager Live: a Hermes gateway platform for full-duplex voice.

The plugin serves a small web page. A phone (or any browser) opens it,
streams its microphone over a WebSocket and plays the replies. Each page
gets its own VoiceManager session, which does speech recognition, speech
synthesis and turn-taking; finished utterances go to Hermes as voice
messages.
"""

from .adapter import register

__all__ = ["register"]
