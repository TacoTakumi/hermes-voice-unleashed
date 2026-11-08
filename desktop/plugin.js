// Voice for a Hermes chat: a composer toggle that opens the microphone for the
// chat in front of you, and a settings page for the voice plugin's WebSocket
// address and access token.

import { atom, Button, host, icons, Input, ListRow, Tip, useValue } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

// The say cutter. The model starts a reply with a spoken summary inside
// <say>...</say>. Fed the reply's streamed text deltas, the cutter gives back
// only the text of the first say block, as it arrives. Tags can be split
// across deltas, so the cutter holds back a tail that could still become a tag
// and lets it go as soon as it cannot. One cutter per reply.
const SAY_OPEN = '<say>'
const SAY_CLOSE = '</say>'

// The length of the longest tail of text that is the start of tag.
function partialTagLength(text, tag) {
  for (let n = Math.min(text.length, tag.length - 1); n > 0; n--) {
    if (text.endsWith(tag.slice(0, n))) return n
  }
  return 0
}

export function createSayCutter() {
  let stage = 'before' // 'before' the block, 'inside' it, or 'done'
  let held = ''

  return {
    // Takes a delta; returns the say text to send now, '' if none.
    push(delta) {
      if (stage === 'done') return ''
      held += delta
      if (stage === 'before') {
        const at = held.indexOf(SAY_OPEN)
        if (at < 0) {
          held = held.slice(held.length - partialTagLength(held, SAY_OPEN))
          return ''
        }
        held = held.slice(at + SAY_OPEN.length)
        stage = 'inside'
      }
      const at = held.indexOf(SAY_CLOSE)
      if (at >= 0) {
        const text = held.slice(0, at)
        held = ''
        stage = 'done'
        return text
      }
      const keep = partialTagLength(held, SAY_CLOSE)
      const text = held.slice(0, held.length - keep)
      held = held.slice(held.length - keep)
      return text
    },
    // Call when the reply is complete. Returns held say text that is not a
    // partial closing tag, and closes the cutter.
    end() {
      const text = stage === 'inside' && !SAY_CLOSE.startsWith(held) ? held : ''
      stage = 'done'
      held = ''
      return text
    }
  }
}

// Voice audio for one chat, the same as the voice page's: the microphone goes
// out as 16 kHz mono 16-bit PCM in 20 ms binary frames, and reply audio comes
// in as binary PCM between audio_start and audio_end frames. The factory opens
// no socket; the caller hands it the two senders and passes on what the socket
// receives.
const MIC_RATE = 16000
const MIC_FRAME = 320 // samples: 20 ms at 16 kHz, 640 bytes

// The capture worklet. The capture context runs at 16 kHz where the browser can
// resample the mic itself; otherwise the worklet decimates from the context
// rate. It is loaded from a blob: URL, so the plugin stays one file.
const MIC_WORKLET = `
class Mic16k extends AudioWorkletProcessor {
  constructor() {
    super()
    this.step = sampleRate / ${MIC_RATE}
    this.pos = 0; this.acc = 0; this.cnt = 0
    this.out = new Int16Array(${MIC_FRAME}); this.n = 0; this.sq = 0
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0]
    if (!ch) return true
    for (let i = 0; i < ch.length; i++) {
      this.acc += ch[i]; this.cnt++; this.pos += 1
      if (this.pos >= this.step) {
        this.pos -= this.step
        const v = Math.max(-1, Math.min(1, this.acc / this.cnt))
        this.acc = 0; this.cnt = 0
        this.sq += v * v
        this.out[this.n++] = v < 0 ? v * 32768 : v * 32767
        if (this.n === this.out.length) {
          this.port.postMessage({ pcm: this.out.buffer, rms: Math.sqrt(this.sq / this.n) }, [this.out.buffer])
          this.out = new Int16Array(${MIC_FRAME}); this.n = 0; this.sq = 0
        }
      }
    }
    return true
  }
}
registerProcessor('mic-16k', Mic16k)
`

// sendBinary(arrayBuffer) and sendJson(frame) go to the voice socket.
// onSpeaking(on) is told when a reply starts and stops being heard.
// Returns:
//   start(mediaStream)  capture the stream and get ready to play. The audio
//                       takes the stream over: stop() ends its tracks.
//   stop()              stop capture and playback, end the stream's tracks and
//                       close the audio contexts. Returns a promise.
//   onBinary(buffer)    reply audio from the socket, an ArrayBuffer of PCM.
//   onJson(frame)       a JSON frame from the socket: audio_start, audio_end
//                       and clear are acted on, others are ignored. Pass
//                       { type: 'clear' } when the socket closes.
export function createVoiceAudio({ sendBinary, sendJson, onSpeaking = () => {} }) {
  let run = 0 // counts starts and stops, so a start that a stop overtook gives up
  let stream = null
  let micCtx = null
  let micNode = null
  let playCtx = null
  // One reply plays at a time. Its edges go back as playback frames: the turn
  // engine needs to know where on the mic stream the reply was heard.
  let reply = null

  function replyBegin(id, rate) {
    replyClear(null)
    if (!playCtx) return
    reply = {
      id, rate, next: playCtx.currentTime + 0.06, first: null, samples: 0, sources: [],
      ended: false, started: false, stopped: false, playing: 0
    }
  }

  function replyPcm(buf) {
    if (!reply || reply.stopped || !playCtx) return
    const pcm = new Int16Array(buf)
    if (!pcm.length) return
    const audio = playCtx.createBuffer(1, pcm.length, reply.rate)
    const ch = audio.getChannelData(0)
    for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768
    const src = playCtx.createBufferSource()
    src.buffer = audio
    src.connect(playCtx.destination)
    const at = Math.max(reply.next, playCtx.currentTime + 0.02)
    src.start(at)
    const r = reply
    if (r.first === null) {
      r.first = at
      setTimeout(() => {
        if (reply !== r || r.stopped) return
        r.started = true
        sendJson({ type: 'playback', event: 'start', request_id: r.id })
        onSpeaking(true)
      }, Math.max(0, (at - playCtx.currentTime) * 1000))
    }
    r.next = at + audio.duration
    r.samples += pcm.length
    r.sources.push(src)
    r.playing += 1
    // The reply finishes when its audio_end has come and no chunk is still
    // playing, in whichever order those two happen.
    src.onended = () => {
      r.playing -= 1
      if (r.ended && r.playing === 0) replyFinish(r, r.samples)
    }
  }

  function replyEnd(id) {
    if (!reply || reply.id !== id) return
    reply.ended = true
    if (reply.playing === 0) replyFinish(reply, reply.samples)
  }

  function replyFinish(r, played) {
    if (r.stopped) return
    r.stopped = true
    if (r.started) {
      sendJson({ type: 'playback', event: 'stop', request_id: r.id, reply_sample: played })
      onSpeaking(false)
    }
    if (reply === r) reply = null
  }

  // Stop every scheduled chunk of the reply at once (a barge-in, or the socket
  // went away). A null id clears whatever reply is playing.
  function replyClear(id) {
    if (!reply || (id && reply.id !== id)) return
    const r = reply
    const played = r.first === null || !playCtx ? 0
      : Math.max(0, Math.min(r.samples, Math.round((playCtx.currentTime - r.first) * r.rate)))
    for (const s of r.sources) {
      try {
        s.onended = null
        s.stop()
      } catch {
        // already stopped
      }
    }
    replyFinish(r, played)
  }

  function release() {
    replyClear(null)
    if (micNode) {
      micNode.port.onmessage = null
      micNode.disconnect()
      micNode = null
    }
    if (stream) {
      for (const track of stream.getTracks()) track.stop()
      stream = null
    }
    const closing = [micCtx, playCtx].filter(Boolean).map(ctx => ctx.close().catch(() => {}))
    micCtx = null
    playCtx = null
    return Promise.all(closing).then(() => {})
  }

  async function start(mediaStream) {
    release()
    const mine = ++run
    stream = mediaStream
    const play = (playCtx = new AudioContext())
    let ctx = null
    let source
    try {
      ctx = new AudioContext({ sampleRate: MIC_RATE })
      source = ctx.createMediaStreamSource(mediaStream)
    } catch {
      if (ctx) void ctx.close().catch(() => {})
      ctx = new AudioContext()
      source = ctx.createMediaStreamSource(mediaStream)
    }
    micCtx = ctx
    const url = URL.createObjectURL(new Blob([MIC_WORKLET], { type: 'text/javascript' }))
    try {
      await play.resume()
      await ctx.audioWorklet.addModule(url)
    } catch (err) {
      if (mine !== run) return
      stop()
      throw err
    } finally {
      URL.revokeObjectURL(url)
    }
    if (mine !== run) return
    const node = new AudioWorkletNode(ctx, 'mic-16k')
    node.port.onmessage = event => {
      if (mine === run) sendBinary(event.data.pcm)
    }
    source.connect(node)
    const sink = ctx.createGain()
    sink.gain.value = 0
    node.connect(sink).connect(ctx.destination)
    micNode = node
  }

  function stop() {
    run++
    return release()
  }

  return {
    start,
    stop,
    onBinary: buf => replyPcm(buf),
    onJson(frame) {
      switch (frame && frame.type) {
        case 'audio_start':
          replyBegin(frame.request_id, frame.sample_rate || 24000)
          break
        case 'audio_end':
          replyEnd(frame.request_id)
          break
        case 'clear':
          replyClear(frame.request_id || null)
          break
      }
    }
  }
}

const ID = 'voicemanager-live'
const DEFAULTS = { address: '', token: '' }

// The live voice state. One microphone and one chat at a time.
const voice = atom({ on: false, sessionId: null, error: '' })
let micStream = null

function stopMic() {
  if (micStream) {
    for (const track of micStream.getTracks()) track.stop()
    micStream = null
  }
}

async function startVoice() {
  const sessionId = host.state.focusedSessionId.get()
  if (!sessionId) {
    host.notify({ kind: 'warning', message: 'Open a chat first, then turn voice on.' })
    return
  }
  try {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true }
    })
  } catch (err) {
    const error = `Microphone refused: ${err && err.name ? err.name : err}`
    voice.set({ on: false, sessionId: null, error })
    host.notify({ kind: 'error', message: error })
    return
  }
  voice.set({ on: true, sessionId, error: '' })
  host.notify({ kind: 'info', message: 'Voice on: microphone open.' })
}

function stopVoice() {
  stopMic()
  voice.set({ on: false, sessionId: null, error: '' })
}

function VoiceToggle() {
  const state = useValue(voice)
  const label = state.on ? 'Turn voice off' : 'Turn voice on'
  return jsx(Tip, {
    label,
    children: jsx(Button, {
      variant: 'ghost',
      size: 'icon',
      'aria-label': label,
      'aria-pressed': state.on,
      onClick: () => (state.on ? stopVoice() : void startVoice()),
      children: jsx(state.on ? icons.Mic : icons.MicOff, {})
    })
  })
}

function SettingRow({ title, description, value, type, placeholder, onSave }) {
  return jsx(ListRow, {
    title,
    description,
    action: jsx(Input, {
      type,
      defaultValue: value,
      placeholder,
      className: 'w-72',
      onBlur: event => onSave(event.target.value.trim())
    })
  })
}

export default {
  id: ID,
  name: 'Voice',
  register(ctx) {
    const prefs = atom({ ...DEFAULTS, ...ctx.storage.get('prefs', {}) })
    const update = patch => {
      prefs.set({ ...prefs.get(), ...patch })
      ctx.storage.set('prefs', prefs.get())
    }

    ctx.register({
      id: 'toggle',
      area: 'composer.actions',
      order: 10,
      render: () => jsx(VoiceToggle, {})
    })

    ctx.registerSettingsPage?.({
      id: 'settings',
      title: 'Voice',
      icon: 'mic',
      render: function VoiceSettings() {
        const { address, token } = useValue(prefs)
        return jsxs('div', {
          children: [
            jsx(SettingRow, {
              title: 'Voice server address',
              description: 'The WebSocket address of the voice plugin, for example ws://host:8765.',
              value: address,
              type: 'text',
              placeholder: 'ws://host:8765',
              onSave: value => update({ address: value })
            }),
            jsx(SettingRow, {
              title: 'Access token',
              description: 'The token the voice plugin is configured with.',
              value: token,
              type: 'password',
              placeholder: 'token',
              onSave: value => update({ token: value })
            })
          ]
        })
      }
    })

    ctx.onDispose(stopVoice)
  }
}
