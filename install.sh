#!/bin/sh
# logspend installer: curl -fsSL https://raw.githubusercontent.com/hamarshehmhmd/logspend/main/install.sh | sh
# Installs the logspend CLI with uv tool > pipx > a private venv in ~/.local/share/logspend. Safe to re-run.
# Env: LOGSPEND_REF=<git ref> (default main)
set -eu
REF="${LOGSPEND_REF:-main}"
SRC="git+https://github.com/hamarshehmhmd/logspend@${REF}"
say() { printf 'logspend-install: %s\n' "$*"; }
die() { say "error: $*" >&2; exit 1; }

if command -v uv >/dev/null 2>&1; then
  say "installing with uv tool ($SRC)"
  uv tool install --force "$SRC" >/dev/null 2>&1 || die "uv tool install failed"
  BIN="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")"
elif command -v pipx >/dev/null 2>&1; then
  say "installing with pipx ($SRC)"
  pipx install --force "$SRC" >/dev/null
  BIN="${PIPX_BIN_DIR:-$HOME/.local/bin}"
else
  PY=""
  for c in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
      PY="$c"; break
    fi
  done
  [ -n "$PY" ] || die "need Python >= 3.10 (or uv / pipx) on PATH"
  command -v git >/dev/null 2>&1 || die "need git to install from GitHub"
  VENV="$HOME/.local/share/logspend/venv"
  say "installing into $VENV with $PY"
  "$PY" -m venv "$VENV"
  "$VENV/bin/python" -m pip install -q --upgrade "$SRC"
  BIN="$HOME/.local/bin"
  mkdir -p "$BIN"
  ln -sf "$VENV/bin/logspend" "$BIN/logspend"
fi

EXE="$BIN/logspend"
[ -x "$EXE" ] || EXE="$(command -v logspend || true)"
[ -n "$EXE" ] && [ -x "$EXE" ] || die "install finished but the logspend command was not found"
say "installed $("$EXE" --version) at $EXE"
