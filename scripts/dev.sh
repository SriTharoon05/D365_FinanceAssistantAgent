#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
mock_mode=false
case "${1:-}" in
  --mock) mock_mode=true ;;
  '') ;;
  *) echo 'Usage: scripts/dev.sh [--mock]' >&2; exit 2 ;;
esac
[[ $# -le 1 ]] || { echo 'Usage: scripts/dev.sh [--mock]' >&2; exit 2; }

if [[ ! -x "$repo_root/backend/.venv/bin/python" || ! -d "$repo_root/frontend/node_modules" ]]; then
  bash "$repo_root/scripts/setup.sh"
fi
"$repo_root/backend/.venv/bin/python" "$repo_root/scripts/check_backend.py" --check-python --existing-venv
[[ -f "$repo_root/backend/.env" ]] || cp "$repo_root/backend/.env.example" "$repo_root/backend/.env"
[[ -f "$repo_root/frontend/.env" ]] || cp "$repo_root/frontend/.env.example" "$repo_root/frontend/.env"

if [[ "$mock_mode" == true ]]; then
  export D365_MOCK_MODE=true
  echo 'DEMO MODE: finance records are synthetic; no Dynamics 365 requests are made.'
fi

cd "$repo_root/backend"
.venv/bin/python "$repo_root/scripts/check_backend.py" --check-port
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload &
backend_pid=$!
cleanup() {
  kill "$backend_pid" 2>/dev/null || true
  wait "$backend_pid" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT TERM

.venv/bin/python "$repo_root/scripts/check_backend.py"
kill -0 "$backend_pid" 2>/dev/null || { echo 'Backend exited during startup; check its logs.' >&2; exit 1; }
echo 'Frontend: http://localhost:5173 | API: http://localhost:8000 | Swagger: http://localhost:8000/docs'
cd "$repo_root/frontend"
npm run dev -- --host localhost --port 5173 --strictPort
