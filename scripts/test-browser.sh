#!/usr/bin/env bash
# Run the page test in headless Chrome. It needs no Hermes checkout.
#   python3 -m venv .venv-test && .venv-test/bin/pip install -r tests/browser/requirements.txt
#   CHROME  path to a Chrome or Chromium binary (default: google-chrome on PATH)
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
cd "$here"
CHROME="${CHROME:-$(command -v google-chrome || command -v chromium || true)}" \
  .venv-test/bin/python -m pytest -c tests/pytest.ini --rootdir tests -p no:cacheprovider tests/browser "$@"
