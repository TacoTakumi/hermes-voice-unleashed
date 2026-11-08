// Voice for a Hermes chat: a composer toggle that opens the microphone for the
// chat in front of you, and a settings page for the voice plugin's WebSocket
// address and access token.

import { atom, Button, host, icons, Input, ListRow, Tip, useValue } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

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
