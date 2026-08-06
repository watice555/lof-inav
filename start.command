#!/bin/bash

set -u

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT" || exit 1

PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
VENV_PY="$ROOT/.venv/bin/python"
REBUILD=0

pause_on_error() {
    if [ -t 0 ]; then
        printf '\nPress Enter to close...'
        read -r _
    fi
}

fail() {
    printf '\nstart.command failed. Check the error above.\n' >&2
    pause_on_error
    exit 1
}

show_help() {
    cat <<'EOF'
Usage:
  ./start.command
  ./start.command --rebuild

The default command creates .venv, installs dependencies, runs a quick
current-only build when data/lof_inav.sqlite3 is missing or incompatible,
then starts the local website.

--rebuild runs the quick current-only build even when the database exists.
EOF
}

case "${1:-}" in
    "") ;;
    --rebuild) REBUILD=1 ;;
    --help|-h)
        show_help
        exit 0
        ;;
    *)
        printf 'Unknown option: %s\n\n' "$1" >&2
        show_help >&2
        exit 2
        ;;
esac

if [ "$#" -gt 1 ]; then
    printf 'Too many arguments.\n\n' >&2
    show_help >&2
    exit 2
fi

if [ ! -d "$ROOT/.venv" ]; then
    PYTHON=""
    if command -v python3 >/dev/null 2>&1; then
        PYTHON="$(command -v python3)"
    elif command -v python >/dev/null 2>&1; then
        PYTHON="$(command -v python)"
    fi

    if [ -z "$PYTHON" ]; then
        printf 'Python was not found. Please install Python 3.10+ and try again.\n' >&2
        fail
    fi

    if ! "$PYTHON" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
        printf 'Python 3.10 or newer is required. Found: ' >&2
        "$PYTHON" --version >&2
        fail
    fi

    printf 'Creating virtual environment...\n'
    "$PYTHON" -m venv "$ROOT/.venv" || fail
fi

if [ ! -x "$VENV_PY" ]; then
    printf 'Virtual environment is incomplete. Delete .venv and run this script again.\n' >&2
    fail
fi

printf 'Installing dependencies...\n'
"$VENV_PY" -m pip install -r "$ROOT/requirements.txt" || fail

if [ "$REBUILD" -eq 1 ]; then
    printf 'Rebuilding current valuation data...\n'
    "$VENV_PY" "$ROOT/build.py" --current-only || fail
elif "$VENV_PY" "$ROOT/scripts/check_database.py" >/dev/null 2>&1; then
    printf 'Local database is ready. Skipping initial build.\n'
else
    printf 'Local database is missing, incomplete, or incompatible. Building current valuation data...\n'
    "$VENV_PY" "$ROOT/build.py" --current-only || fail
fi

"$VENV_PY" "$ROOT/scripts/check_database.py" || fail

printf 'Starting LOF iNAV...\n'
LOF_INAV_OPEN_BROWSER=1 "$VENV_PY" "$ROOT/serve.py"
server_status=$?
if [ "$server_status" -ne 0 ] && [ "$server_status" -ne 130 ] && [ "$server_status" -ne 143 ]; then
    fail
fi

printf '\nServer stopped.\n'
