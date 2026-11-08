// Voice for a Hermes chat: a composer toggle that attaches the chat in front of
// you to the voice plugin's server (you speak to the chat, and the spoken part
// of each reply is read out), and a settings page for the server's WebSocket
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
  // Replies play one after another. One turn can bring several (one per
  // assistant segment, and one per typed message), and speech is made faster
  // than it plays, so a reply can begin while the one before is still heard:
  // it is queued behind it. Binary frames carry no request id, so reply audio
  // belongs to the reply begun last (intake). Each reply's edges go back as
  // playback frames: the turn engine needs to know where on the mic stream
  // the reply was heard.
  let replies = [] // queued in play order; replies[0] is the one heard
  let intake = null
  let speaking = false
  // Each playback frame carries mic_sample: the index on the mic stream sent
  // on this connection where the edge reached the speaker. The mic samples
  // sent so far are counted, and each sent frame anchors that count to the
  // page clock at the moment the sound entered the mic. The edge's speaker
  // time on the page clock is then mapped onto the count through the anchor.
  let micSent = 0 // 16 kHz samples sent since start()
  let micAnchor = null // { count, time }: micSent when a frame went out, and its capture time
  let captureLatencyMs = 0

  // The page clock time (performance.now() ms) at which a playCtx time is heard.
  function speakerTime(t) {
    const ctx = playCtx
    if (!ctx) return performance.now()
    if (typeof ctx.getOutputTimestamp === 'function') {
      const ts = ctx.getOutputTimestamp()
      if (ts && ts.contextTime > 0 && ts.performanceTime > 0) {
        return ts.performanceTime + (t - ts.contextTime) * 1000
      }
    }
    const latency = ctx.outputLatency ?? ctx.baseLatency ?? 0
    return performance.now() + (t - ctx.currentTime + latency) * 1000
  }

  // The mic sample index at a page clock time.
  function micSampleAt(perfMs) {
    if (!micAnchor) return micSent
    return Math.max(0, Math.round(micAnchor.count + (perfMs - micAnchor.time) / 1000 * MIC_RATE))
  }

  function replyBegin(id, rate) {
    if (!playCtx) return
    // A reply that has not had its audio_end takes no more audio: what comes
    // now is the new reply's. What it has queued still plays.
    if (intake) replyEnd(intake)
    const tail = replies[replies.length - 1]
    const r = {
      id, rate, next: Math.max(playCtx.currentTime + 0.06, tail ? tail.next : 0), first: null, samples: 0,
      sources: [], ended: false, due: false, started: false, stopped: false, playing: 0
    }
    replies.push(r)
    intake = r
  }

  function replyPcm(buf) {
    const r = intake
    if (!r || !playCtx) return
    const pcm = new Int16Array(buf)
    if (!pcm.length) return
    const audio = playCtx.createBuffer(1, pcm.length, r.rate)
    const ch = audio.getChannelData(0)
    for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768
    const src = playCtx.createBufferSource()
    src.buffer = audio
    src.connect(playCtx.destination)
    const at = Math.max(r.next, playCtx.currentTime + 0.02)
    src.start(at)
    if (r.first === null) {
      r.first = at
      setTimeout(() => {
        if (r.stopped) return
        r.due = true
        replyAudible()
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

  // The reply at the head of the queue whose first chunk is due is now heard.
  // A reply behind it waits for its stop frame, so the start and stop frames of
  // two replies never overlap.
  function replyAudible() {
    const r = replies[0]
    if (!r || !r.due || r.started) return
    r.started = true
    r.startSample = micSampleAt(speakerTime(r.first))
    sendJson({ type: 'playback', event: 'start', request_id: r.id, mic_sample: r.startSample })
    syncSpeaking()
  }

  function replyEnd(r) {
    if (!r || r.ended) return
    r.ended = true
    if (intake === r) intake = null
    if (r.playing === 0) replyFinish(r, r.samples)
  }

  function replyFinish(r, played) {
    if (r.stopped) return
    r.stopped = true
    replies = replies.filter(x => x !== r)
    if (intake === r) intake = null
    if (r.started) {
      // The edge is the end of the last sample heard: the scheduled end of the
      // last chunk when the reply played out (a late chunk leaves a gap, and
      // the end lies after it), now when the reply was cut while it still played.
      const edge = playCtx ? Math.min(r.next, playCtx.currentTime) : r.next
      const micSample = Math.max(r.startSample, micSampleAt(speakerTime(edge)))
      sendJson({ type: 'playback', event: 'stop', request_id: r.id, reply_sample: played, mic_sample: micSample })
    }
    replyAudible()
    syncSpeaking()
  }

  // Speaking from the first reply heard until the last one stops: across the
  // short gap between two queued replies it stays on while the next reply has
  // audio scheduled.
  function syncSpeaking() {
    const on = replies.some(r => r.started) || (speaking && replies.some(r => r.first !== null))
    if (on === speaking) return
    speaking = on
    onSpeaking(on)
  }

  // Stop every scheduled chunk at once (a barge-in, or the socket went away).
  // A clear stops the queued replies too, not only the one it names: a
  // barge-in carries the id of the reply being cut, and the user talked over
  // the speech, so what was queued behind it is not wanted either. A null id
  // clears whatever is playing; an id that is not queued changes nothing.
  function replyClear(id) {
    if (id && !replies.some(r => r.id === id)) return
    const all = replies
    replies = []
    intake = null
    for (const r of all) {
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
    micSent = 0
    micAnchor = null
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
    // How long the mic's sound takes to reach the page: the track's own
    // latency when it says, else the capture context's.
    let trackLatency
    try {
      trackLatency = mediaStream.getAudioTracks()[0]?.getSettings?.().latency
    } catch {
      trackLatency = undefined
    }
    captureLatencyMs = (typeof trackLatency === 'number' ? trackLatency : ctx.baseLatency || 0) * 1000
    const node = new AudioWorkletNode(ctx, 'mic-16k')
    node.port.onmessage = event => {
      if (mine !== run) return
      sendBinary(event.data.pcm)
      micSent += event.data.pcm.byteLength / 2
      micAnchor = { count: micSent, time: performance.now() - captureLatencyMs }
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
          replyEnd(replies.findLast(r => r.id === frame.request_id))
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
const MAX_ATTACH_ID = 256 // the voice server refuses longer attach ids
const REPLACED_CLOSE = 4001 // another client took this chat's voice over
const KEYS_KEPT = 64 // finished and cut-off reply keys a session remembers
const SEEN_KEPT = 64 // segments whose streamed length a session remembers

const MOVED = { kind: 'info', message: 'Voice off: you moved to another chat.' }
const REPLACED = { kind: 'warning', message: 'Voice off: voice for this chat moved to another window.' }

// The voice server's attach address for a stored session id. The address may
// be given with or without /ws, and as http(s) for ws(s).
function attachUrl(address, token, storedId) {
  let base = address.trim().replace(/\/+$/, '').replace(/^http(s?):\/\//i, 'ws$1://')
  if (!/\/ws$/.test(base)) base += '/ws'
  return `${base}?token=${encodeURIComponent(token)}&attach=${encodeURIComponent(storedId)}`
}

// A set that forgets its oldest keys past KEYS_KEPT.
function keySet() {
  const keys = new Set()
  return {
    has: key => keys.has(key),
    delete: key => keys.delete(key),
    add(key) {
      keys.delete(key)
      keys.add(key)
      while (keys.size > KEYS_KEPT) keys.delete(keys.values().next().value)
    }
  }
}

const errorText = err => (err && (err.message || err.name)) || String(err)

// Live voice for the chat in front of you, one chat at a time. Turning voice
// on takes the chat's two ids at that moment: the runtime id, which
// prompt.submit takes, and the stored id, which the voice server attaches to
// (the gateway's hooks see that one). Each spoken utterance goes to the chat
// as a voice turn, and the <say> block of each reply streamed in that chat,
// typed or spoken, goes back to the server as say frames.
//
// A reply's key is its turn id and segment: each assistant message of a turn
// (the text before and after tool calls) is its own reply to the server. When
// the server clears playback or hears a new utterance, the reply in flight is
// cut off: nothing more of it is sent, not even its say_end, and the rest of
// its turn is held back.
//
// An utterance sent while a turn runs is usually taken into that turn: Hermes
// redirects it (the model request is cancelled and the answer streams on in
// the same turn and segment, with no event in between) or steers it (the
// answer is the text after the running tools). prompt.submit's answer says
// which, and only it marks the change: whatever the turn streams before it is
// the old answer. Once prompt.submit answers 'redirected' or 'steered', the
// turn speaks again under keys of their own, each with a fresh say cutter that
// starts where its segment had got to; any other answer means a turn of its
// own answers the utterance, and the cut turn stays silent.
//
// Each utterance and clear frame names the server's epoch, and every say and
// say_end carries the last epoch heard. Frames already on the way when the
// server cut a reply name an older epoch, and the server drops them.
function createLiveVoice(ctx, prefs) {
  const state = atom({ status: 'off' }) // 'off', 'connecting' or 'on'
  let live = null // the running session
  let localTurns = 0

  const localTurn = () => `local-${Date.now().toString(36)}-${++localTurns}`
  const isOpen = s => live === s && s.ws && s.ws.readyState === WebSocket.OPEN

  function sendJson(s, frame) {
    if (isOpen(s)) s.ws.send(JSON.stringify(frame))
  }

  async function start() {
    if (live) return
    const { address, token } = prefs.get()
    if (!address || !token) {
      host.notify({ kind: 'warning', message: 'Set the voice server address and access token in the Voice settings first.' })
      return
    }
    const runtime = host.state.focusedSessionId.get()
    const stored = host.state.focusedStoredSessionId.get()
    if (!runtime || !stored) {
      host.notify({ kind: 'warning', message: 'Open a chat first, then turn voice on.' })
      return
    }
    if (stored.length > MAX_ATTACH_ID) {
      host.notify({ kind: 'error', message: 'Voice cannot attach to this chat: its id is too long.' })
      return
    }
    const s = {
      runtime, stored, address, token, ws: null, stream: null, audio: null, capturing: false,
      errored: false, turn: null, reply: null, dead: keySet(), done: keySet(), epoch: 0,
      cuts: 0, resumed: null, seen: new Map()
    }
    live = s
    state.set({ status: 'connecting' })
    let stream
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true }
      })
    } catch (err) {
      end(s, { kind: 'error', message: `Microphone refused: ${err && err.name ? err.name : err}` })
      return
    }
    if (live !== s) {
      for (const track of stream.getTracks()) track.stop()
      return
    }
    s.stream = stream
    s.audio = createVoiceAudio({
      sendBinary: buf => {
        if (isOpen(s)) s.ws.send(buf)
      },
      sendJson: frame => sendJson(s, frame)
    })
    connect(s)
  }

  function connect(s) {
    const ws = new WebSocket(attachUrl(s.address, s.token, s.stored))
    ws.binaryType = 'arraybuffer'
    s.ws = ws
    s.epoch = 0 // a new voice session counts from the start
    ws.onopen = () => {
      if (live !== s || s.ws !== ws || s.capturing) return
      s.capturing = true
      s.audio.start(s.stream).catch(err => {
        end(s, { kind: 'error', message: `Voice off: microphone capture failed: ${errorText(err)}` })
      })
    }
    ws.onmessage = event => {
      if (live !== s || s.ws !== ws) return
      if (typeof event.data !== 'string') {
        s.audio.onBinary(event.data)
        return
      }
      let frame
      try {
        frame = JSON.parse(event.data)
      } catch {
        return // not a frame of ours
      }
      onFrame(s, frame)
    }
    ws.onclose = event => {
      if (live !== s || s.ws !== ws) return
      if (event.code === REPLACED_CLOSE) end(s, REPLACED)
      else if (s.errored) end(s, null)
      else end(s, { kind: 'warning', message: `Voice off: the voice server closed the connection (code ${event.code}).` })
    }
  }

  function onFrame(s, frame) {
    switch (frame && frame.type) {
      case 'ready':
        state.set({ status: 'on' })
        break
      case 'error':
        s.errored = true
        host.notify({ kind: 'error', message: `Voice: ${frame.message || 'the voice server reported an error.'}` })
        break
      case 'utterance': {
        const turn = cut(s)
        takeEpoch(s, frame)
        submit(s, frame.text, turn)
        break
      }
      case 'clear':
        cut(s)
        takeEpoch(s, frame)
        s.audio.onJson(frame)
        break
      case 'replaced':
        end(s, REPLACED)
        break
      default:
        s.audio.onJson(frame)
    }
  }

  // turn is the turn the utterance cut, if one was in flight.
  function submit(s, text, turn) {
    if (typeof text !== 'string' || !text.trim()) return
    const cuts = s.cuts
    host.request('prompt.submit', { session_id: s.runtime, text, voice_turn: true }).then(
      result => {
        const status = result && result.status
        if (turn && live === s && s.cuts === cuts && (status === 'redirected' || status === 'steered')) {
          resume(s, turn, cuts)
        }
      },
      err => {
        if (live === s) host.notify({ kind: 'error', message: `Voice: what you said did not reach the chat: ${errorText(err)}` })
      }
    )
  }

  // Hermes took the utterance into the cut turn: the rest of that turn answers
  // it. Only the latest cut resumes a turn; an earlier utterance's answer was
  // itself cut.
  function resume(s, turn, cuts) {
    s.dead.delete(turn)
    s.resumed = { turn, cuts }
  }

  // A server without epochs sends none: the last one stays.
  function takeEpoch(s, frame) {
    if (Number.isInteger(frame.epoch) && frame.epoch >= 0) s.epoch = frame.epoch
  }

  // The reply in flight takes no more say text, and its turn is held back.
  // Returns the turn cut, or null.
  function cut(s) {
    s.cuts++
    const turn = (s.reply && s.reply.turn) || s.turn
    if (s.reply) {
      s.dead.add(s.reply.turn)
      s.done.add(s.reply.key)
      s.reply = null
    }
    if (s.turn) s.dead.add(s.turn)
    return turn || null
  }

  // The reply in flight is over: the rest of its say text, then say_end.
  function finishReply(s) {
    const r = s.reply
    if (!r) return
    s.reply = null
    s.done.add(r.key)
    if (s.dead.has(r.turn)) return
    const tail = r.cutter.end()
    if (tail) sendJson(s, { type: 'say', text: tail, reply: r.key, epoch: s.epoch })
    sendJson(s, { type: 'say_end', reply: r.key, epoch: s.epoch })
  }

  function replyDelta(s, payload) {
    const text = typeof payload.text === 'string' ? payload.text : ''
    const turn = typeof payload.turn_id === 'string' && payload.turn_id ? payload.turn_id : (s.turn ||= localTurn())
    const segment = Number.isInteger(payload.segment) && payload.segment >= 0 ? payload.segment : 0
    const base = `${turn}:${segment}`
    const stamped = Number.isInteger(payload.offset) && payload.offset >= 0
    // How far this segment had got before this delta, held back text included.
    // Kept per segment, so a replay of any earlier segment is not spoken again.
    const seen = s.seen.get(base) ?? 0
    if (stamped) {
      s.seen.delete(base)
      s.seen.set(base, Math.max(seen, payload.offset + text.length))
      if (s.seen.size > SEEN_KEPT) s.seen.delete(s.seen.keys().next().value)
    }
    if (s.dead.has(turn)) return
    const key = s.resumed && s.resumed.turn === turn ? `${base}:${s.resumed.cuts}` : base
    if (s.done.has(key)) return
    if (!s.reply || s.reply.key !== key) {
      finishReply(s)
      s.reply = { key, turn, cutter: createSayCutter(), end: seen }
    }
    const r = s.reply
    let fresh = text
    // A stamped delta can come twice (a replay after a reconnect): take only
    // the part past what this reply has had. A reply that starts mid-segment
    // (the answer after a redirect) has had what came before it.
    if (stamped) {
      const end = payload.offset + text.length
      if (end <= r.end) return
      fresh = text.slice(Math.max(0, r.end - payload.offset))
      r.end = end
    }
    const say = r.cutter.push(fresh)
    if (say) sendJson(s, { type: 'say', text: say, reply: key, epoch: s.epoch })
  }

  // Gateway events carry the runtime session id; only the attached chat's count.
  function onReplyEvent(event) {
    const s = live
    if (!s || !event || event.session_id !== s.runtime) return
    const payload = event.payload || {}
    switch (event.type) {
      case 'message.start':
        finishReply(s)
        s.turn = typeof payload.turn_id === 'string' && payload.turn_id ? payload.turn_id : localTurn()
        break
      case 'message.delta':
        replyDelta(s, payload)
        break
      case 'message.interim':
        finishReply(s)
        break
      case 'message.complete':
        finishReply(s)
        s.turn = null
        break
    }
  }

  function dropSocket(s) {
    const ws = s.ws
    s.ws = null
    if (!ws) return
    ws.onopen = ws.onmessage = ws.onclose = null
    try {
      ws.close(1000)
    } catch {
      // already closed
    }
  }

  // End the session: no more frames either way, no more submits for its chat.
  function end(s, notice) {
    if (live !== s) return
    live = null
    dropSocket(s)
    if (s.audio) {
      s.audio.onJson({ type: 'clear' })
      void s.audio.stop()
    }
    if (s.stream) for (const track of s.stream.getTracks()) track.stop()
    state.set({ status: 'off' })
    if (notice) host.notify(notice)
  }

  function stop() {
    if (live) end(live, null)
  }

  // The stored id moved under the same chat (context compression starts a new
  // stored session): attach again with the new one.
  function reattach(s, stored) {
    s.stored = stored
    if (!s.audio) return // still opening the mic; connect() takes the new id
    cut(s)
    dropSocket(s)
    s.audio.onJson({ type: 'clear' })
    s.errored = false
    state.set({ status: 'connecting' })
    connect(s)
  }

  // Both ids change when you move to another chat, in either order. A runtime
  // change ends the session at once; a stored id change is looked at once the
  // current change is over, so a chat switch never attaches to the new chat.
  function checkStored() {
    const s = live
    if (!s) return
    if (host.state.focusedSessionId.get() !== s.runtime) {
      end(s, MOVED)
      return
    }
    const stored = host.state.focusedStoredSessionId.get()
    if (stored && stored !== s.stored && stored.length <= MAX_ATTACH_ID) reattach(s, stored)
  }

  const listen = (type, fn) => (ctx.onEvent ? ctx.onEvent(type, fn) : host.onEvent(type, fn))
  for (const type of ['message.start', 'message.delta', 'message.interim', 'message.complete']) {
    listen(type, onReplyEvent)
  }
  const unsubscribe = [
    host.state.focusedSessionId.subscribe(id => {
      if (live && id !== live.runtime) end(live, MOVED)
    }),
    host.state.focusedStoredSessionId.subscribe(id => {
      if (live && id !== live.stored) setTimeout(checkStored, 0)
    })
  ]

  return {
    state,
    start,
    stop,
    dispose() {
      stop()
      for (const off of unsubscribe) off()
    }
  }
}

function VoiceToggle({ voice }) {
  const { status } = useValue(voice.state)
  const label = status === 'off' ? 'Turn voice on' : 'Turn voice off'
  const icon = status === 'on' ? icons.Mic : status === 'connecting' ? icons.Loader2 || icons.Mic : icons.MicOff
  return jsx(Tip, {
    label: status === 'connecting' ? 'Voice connecting. Click to stop.' : label,
    children: jsx(Button, {
      variant: 'ghost',
      size: 'icon',
      'aria-label': label,
      'aria-pressed': status !== 'off',
      onClick: () => (status === 'off' ? void voice.start() : voice.stop()),
      children: jsx(icon, {})
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

    const voice = createLiveVoice(ctx, prefs)

    ctx.register({
      id: 'toggle',
      area: 'composer.actions',
      order: 10,
      render: () => jsx(VoiceToggle, { voice })
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

    ctx.onDispose(voice.dispose)
  }
}
