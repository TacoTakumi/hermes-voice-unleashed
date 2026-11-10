#!/usr/bin/env bash
# Live check: a throwaway Hermes gateway with this plugin, your VoiceManager and
# your model; one warm-up question, then five spoken questions, timed from the end
# of turn to the first reply audio. Nothing outside a temp directory is changed.
#
# Needs:
#   VOICE_UNLEASHED_VOICEMANAGER_TOKEN  a VoiceManager bearer token
#   LIVE_MODEL                 model name on the OpenAI-compatible endpoint
# Optional:
#   LIVE_BASE_URL              default http://127.0.0.1:8080/v1
#   VOICE_UNLEASHED_VOICEMANAGER_URL    default http://127.0.0.1:8255
#   LIVE_QUESTION              default "What is the capital of France?"
#   LIVE_RUNS                  default 5
#   HERMES_CHECKOUT, HERMES_PYTHON  as for scripts/test.sh
#   LIVE_PYTHON                python with websockets and httpx (default .venv-test)
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
checkout="${HERMES_CHECKOUT:-$here/../hermes-agent}"
hermes_python="${HERMES_PYTHON:-$checkout/.venv/bin/python}"
live_python="${LIVE_PYTHON:-$here/.venv-test/bin/python}"
: "${VOICE_UNLEASHED_VOICEMANAGER_TOKEN:?set VOICE_UNLEASHED_VOICEMANAGER_TOKEN}"
: "${LIVE_MODEL:?set LIVE_MODEL}"
base_url="${LIVE_BASE_URL:-http://127.0.0.1:8080/v1}"
vm_url="${VOICE_UNLEASHED_VOICEMANAGER_URL:-http://127.0.0.1:8255}"
port=18765
page_token="live-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')"

scratch="$(mktemp -d -t voice-unleashed-live.XXXXXX)"
gateway_pid=""
cleanup() {
  if [ -n "$gateway_pid" ] && kill -0 "$gateway_pid" 2>/dev/null; then
    kill "$gateway_pid"; wait "$gateway_pid" 2>/dev/null || true
  fi
  rm -r -- "$scratch"
}
trap cleanup EXIT

home="$scratch/hermes"
mkdir -p "$home/plugins"
ln -s "$here" "$home/plugins/voice-unleashed"
cat > "$home/config.yaml" <<YAML
model:
  default: $LIVE_MODEL
  provider: custom
  base_url: $base_url
  api_mode: chat_completions
plugins:
  enabled:
    - voice-unleashed
YAML

echo "starting a gateway in $home"
HERMES_HOME="$home" PYTHONPATH="$checkout" \
  VOICE_UNLEASHED_TOKEN="$page_token" VOICE_UNLEASHED_VOICEMANAGER_URL="$vm_url" \
  VOICE_UNLEASHED_VOICEMANAGER_TOKEN="$VOICE_UNLEASHED_VOICEMANAGER_TOKEN" VOICE_UNLEASHED_PORT="$port" \
  VOICE_UNLEASHED_ALLOWED_USERS=owner \
  systemd-run --user --scope --quiet -p MemoryMax=8G \
  "$hermes_python" -m hermes_cli.main gateway run > "$scratch/gateway.log" 2>&1 &
gateway_pid=$!

for _ in $(seq 1 120); do
  if curl -s -o /dev/null "http://127.0.0.1:$port/?token=$page_token"; then break; fi
  if ! kill -0 "$gateway_pid" 2>/dev/null; then
    echo "the gateway exited:"; tail -40 "$scratch/gateway.log"; exit 1
  fi
  sleep 1
done
curl -sf -o /dev/null "http://127.0.0.1:$port/?token=$page_token" || {
  echo "the voice page did not come up:"; tail -40 "$scratch/gateway.log"; exit 1; }

"$live_python" "$here/scripts/live_check.py" make-wav --vm "$vm_url" \
  --vm-token "$VOICE_UNLEASHED_VOICEMANAGER_TOKEN" \
  --text "${LIVE_QUESTION:-What is the capital of France?}" --out "$scratch/question.wav"
status=0
"$live_python" "$here/scripts/live_check.py" run --page "ws://127.0.0.1:$port" --token "$page_token" \
  --wav "$scratch/question.wav" --runs "${LIVE_RUNS:-5}" || status=$?
echo "--- gateway log, voice lines"
grep -hE "voice page|voice_unleashed|reply audio|Voice turn|reasoning" "$home"/logs/*.log "$scratch/gateway.log" 2>/dev/null | tail -30 || true
exit "$status"
