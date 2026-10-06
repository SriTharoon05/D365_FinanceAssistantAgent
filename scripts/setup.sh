#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
backend_dir="$repo_root/backend"
frontend_dir="$repo_root/frontend"

command -v npm >/dev/null || { echo 'Node.js 22+ and npm are required.' >&2; exit 1; }

if [[ -d "$backend_dir/.venv" && ! -x "$backend_dir/.venv/bin/python" ]]; then
  echo 'backend/.venv is incomplete. Stop servers, remove only backend/.venv, and rerun setup with Python 3.13.3.' >&2
  exit 1
fi

if [[ ! -x "$backend_dir/.venv/bin/python" ]]; then
  if command -v python3.13 >/dev/null; then
    python_command=python3.13
  elif command -v python3 >/dev/null; then
    python_command=python3
  else
    echo 'Install Python 3.13.3 before running setup.' >&2
    exit 1
  fi
  "$python_command" "$repo_root/scripts/check_backend.py" --check-python
  "$python_command" -m venv "$backend_dir/.venv"
fi

"$backend_dir/.venv/bin/python" "$repo_root/scripts/check_backend.py" --check-python --existing-venv
"$backend_dir/.venv/bin/python" -m pip install --require-hashes -r "$backend_dir/requirements.lock"

[[ -f "$backend_dir/.env" ]] || cp "$backend_dir/.env.example" "$backend_dir/.env"
[[ -f "$frontend_dir/.env" ]] || cp "$frontend_dir/.env.example" "$frontend_dir/.env"

(cd "$backend_dir" && .venv/bin/python -m alembic upgrade head)
(cd "$frontend_dir" && npm ci)

echo 'Setup complete. Add credentials to backend/.env, or start with scripts/dev.sh --mock.'
