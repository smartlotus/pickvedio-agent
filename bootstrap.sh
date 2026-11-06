#!/usr/bin/env bash
# One-shot setup for an agent's own machine -- the path Muse's Secure VM takes
# when you hand it this repository and say "run it locally".
#
# Why a script instead of prose: an agent following instructions in a terminal
# gets the venv name, the extras and the self-test order right about once in
# several tries, and a half-installed environment fails later with a message
# that looks like a bug in this software. This script makes the setup step
# deterministic and its output a single honest verdict.
#
# Usage:
#   bash bootstrap.sh                # install into ./.venv, run the self-test
#   bash bootstrap.sh --with-service # also install fastapi/uvicorn for hosting
#   PVC_VENV=/path bash bootstrap.sh # choose the environment location
set -euo pipefail

WITH_SERVICE=0
for arg in "$@"; do
  case "$arg" in
    --with-service) WITH_SERVICE=1 ;;
    -h | --help)
      sed -n '2,20p' "$0"
      exit 0
      ;;
    *)
      echo "unknown argument: $arg (try --help)" >&2
      exit 2
      ;;
  esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
VENV="${PVC_VENV:-$ROOT/.venv}"
EXTRAS="dev"
if [ "$WITH_SERVICE" = "1" ]; then
  EXTRAS="dev,service"
fi

PYTHON_BIN="${PYTHON_BIN:-}"
if [ -z "$PYTHON_BIN" ]; then
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      PYTHON_BIN="$candidate"
      break
    fi
  done
fi
if [ -z "$PYTHON_BIN" ]; then
  echo "verdict: FAILED -- no python3/python on PATH" >&2
  exit 1
fi

# 3.10 is the floor the package declares; the enum/dataclass interaction that
# broke on older toolchains is why, so say so instead of failing obscurely.
if ! "$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "verdict: FAILED -- needs Python >= 3.10, found $("$PYTHON_BIN" -V 2>&1)" >&2
  exit 1
fi

echo "==> interpreter: $("$PYTHON_BIN" -V 2>&1) at $(command -v "$PYTHON_BIN")"
echo "==> environment: $VENV"
VENV_PY="$VENV/bin/python"
[ -x "$VENV_PY" ] || VENV_PY="$VENV/Scripts/python.exe"

if [ ! -x "$VENV_PY" ]; then
  if venv_error="$("$PYTHON_BIN" -m venv "$VENV" 2>&1)"; then
    :
  else
    # Debian and Ubuntu ship python3 without ensurepip (it lives in the
    # python3-venv package), so the very first command an agent runs in a cloud
    # VM dies with a message about pip being unavailable, which reads like a bug
    # in this repo and is not. Fall back to a venv without pip and install pip
    # from PyPA's own bootstrap script.
    echo "    plain 'python3 -m venv' failed: ${venv_error:0:300}" >&2
    echo "==> retrying: venv --without-pip, then get-pip.py"
    "$PYTHON_BIN" -m venv --without-pip "$VENV"
    if command -v curl >/dev/null 2>&1; then
      curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "$VENV/get-pip.py"
    else
      "$PYTHON_BIN" -c 'import urllib.request,sys; urllib.request.urlretrieve("https://bootstrap.pypa.io/get-pip.py", sys.argv[1])' "$VENV/get-pip.py"
    fi
    if [ ! -s "$VENV/get-pip.py" ]; then
      echo "verdict: FAILED -- no pip in this python and get-pip.py could not be fetched." >&2
      echo "                Fix: apt-get install -y python3-venv python3-pip, then re-run bash bootstrap.sh" >&2
      exit 1
    fi
    "$VENV_PY" "$VENV/get-pip.py" --quiet --disable-pip-version-check || {
      echo "verdict: FAILED -- get-pip.py ran but pip is still missing in $VENV." >&2
      echo "                Fix: apt-get install -y python3-venv, then re-run bash bootstrap.sh" >&2
      exit 1
    }
    rm -f "$VENV/get-pip.py"
    echo "    pip bootstrapped into $VENV"
  fi
fi

echo "==> installing .[$EXTRAS]"
"$VENV_PY" -m pip install --quiet --disable-pip-version-check --upgrade pip
"$VENV_PY" -m pip install --quiet --disable-pip-version-check -e ".[$EXTRAS]"

VENV_BIN="$VENV/bin"
[ -d "$VENV_BIN" ] || VENV_BIN="$VENV/Scripts"

echo "==> self-test (offline: no network, no model calls)"
# Written under the repo, not /tmp: on Windows an MSYS /tmp is not the directory
# a Windows-built Python resolves, so the report can be written and then read as
# missing.
CHECK_JSON="$ROOT/.bootstrap-check.json"
STATUS_JSON="$ROOT/.bootstrap-status.json"
"$VENV_PY" -c 'import pickvedio_core; print("    pickvedio_core", pickvedio_core.__version__)'
if ! "$VENV_BIN/pvc" check >"$CHECK_JSON" 2>/dev/null; then
  echo "verdict: FAILED -- pvc check reported failures; see $CHECK_JSON" >&2
  cat "$CHECK_JSON" >&2 || true
  exit 1
fi
"$VENV_PY" - "$CHECK_JSON" <<'VERIFY'
import json, sys
doc = json.load(open(sys.argv[1], encoding="utf-8"))
assert doc["ok"], doc
assert doc["data"]["network_used"] is False and doc["data"]["model_calls"] == 0
failed = [c["name"] for c in doc["data"]["checks"] if not c["passed"]]
assert not failed, failed
print("    self-test passed:", len(doc["data"]["checks"]), "checks")
VERIFY

echo "==> configuration probe (prints names and booleans, never values)"
"$VENV_BIN/pvc" status >"$STATUS_JSON" 2>/dev/null || true
"$VENV_PY" - "$STATUS_JSON" <<'STATUS'
import json, sys
try:
    doc = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    print("    status unreadable")
    raise SystemExit(0)
data = doc.get("data", {})
missing = data.get("missing") or []
print("    llm:", data.get("llm", {}).get("provider"), data.get("llm", {}).get("model") or "(unset)")
print("    channels:", ", ".join(k for k, v in (data.get("channels") or {}).items() if v.get("available")))
if missing:
    print("    still needed:")
    for item in missing:
        print("      -", item)
else:
    print("    nothing missing: this machine can summarise now")
STATUS

cat <<NEXT

verdict: READY

Next steps, in the order that costs nothing first:

  export PATH="$VENV_BIN:\$PATH"
  export PVC_LLM_PROVIDER=deepseek PVC_LLM_API_KEY=<your key>   # or another preset

  pvc resolve --url "<paste anything containing the link>"
  pvc fetch --url "<canonical url>"                  # subtitle channel; expect
                                                     # needs_browser when logged out
  pvc push --url "<canonical url>" --transcript relay.json --style brief

To get relay.json: open the video page in this machine's browser, run
skills/video-summary/assets/browser-extract.js in that page's console (or via
CDP Runtime.evaluate), and save the object it returns. That is the channel that
works when captions are gated, and it needs no audio download and no ASR.

NEXT
