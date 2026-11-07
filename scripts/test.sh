#!/usr/bin/env bash
# Run the plugin tests against a Hermes checkout.
#   HERMES_CHECKOUT  path to a hermes-agent checkout (default: ../hermes-agent)
#   HERMES_PYTHON    python with Hermes's dependencies (default: its .venv)
# Extra arguments go to pytest, e.g. scripts/test.sh tests/test_server.py -q
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
checkout="${HERMES_CHECKOUT:-$here/../hermes-agent}"
python="${HERMES_PYTHON:-$checkout/.venv/bin/python}"
scratch="$(mktemp -d -t vmlive-test.XXXXXX)"
trap 'rm -r -- "$scratch"' EXIT
cd "$here"
HERMES_HOME="$scratch" PYTHONPATH="$checkout${PYTHONPATH:+:$PYTHONPATH}" \
  "$python" -m pytest -c tests/pytest.ini --rootdir tests -p no:cacheprovider "$@"
