#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
backend_dir="$repo_root/backend"
frontend_dir="$repo_root/frontend"

command -v npm >/dev/null || { echo 'Node.js 22+ and npm are required.' >&2; exit 1; }

if [[ ! -x "$backend_dir/.venv/bin/python" ]]; then
  if command -v python3.12 >/dev/null; then
    python_command=python3.12
  elif command -v python3 >/dev/null; then
    python_command=python3
  else
    echo 'Python 3.12+ is required.' >&2
    exit 1
  fi
  "$python_command" -c 'import sys; assert sys.version_info >= (3, 12), "Python 3.12+ is required"'
  "$python_command" -m venv "$backend_dir/.venv"
fi

"$backend_dir/.venv/bin/python" -c 'import sys; assert sys.version_info >= (3, 12), "Python 3.12+ is required"'
"$backend_dir/.venv/bin/python" -m pip install --require-hashes -r "$backend_dir/requirements.lock"

[[ -f "$backend_dir/.env" ]] || cp "$backend_dir/.env.example" "$backend_dir/.env"
[[ -f "$frontend_dir/.env" ]] || cp "$frontend_dir/.env.example" "$frontend_dir/.env"

(cd "$backend_dir" && .venv/bin/python -m alembic upgrade head)
(cd "$frontend_dir" && npm ci)

echo 'Setup complete. Add credentials to backend/.env, or start with scripts/dev.sh --mock.'
