"""Local launcher checks; no external dependencies or credential access."""

import argparse
import json
import socket
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener


def check_port(host: str, port: int) -> int:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind((host, port))
    except OSError:
        print(
            f"Backend port {port} is already in use. Stop the existing server before starting this one.",
            file=sys.stderr,
        )
        return 1
    return 0


def wait_for_health(host: str, port: int, timeout: float) -> int:
    deadline = time.monotonic() + timeout
    # Local readiness checks should not travel through a corporate HTTP proxy.
    opener = build_opener(ProxyHandler({}))
    while time.monotonic() < deadline:
        try:
            with opener.open(f"http://{host}:{port}/api/health", timeout=1) as response:
                result = json.loads(response.read(16_384))
            if result.get("api") == "healthy" and result.get("database") == "healthy":
                return 0
        except (HTTPError, URLError, OSError, ValueError):
            pass
        time.sleep(0.25)
    print(
        "Backend did not become healthy in time. Check its startup logs; the frontend was not started.",
        file=sys.stderr,
    )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-port", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    if args.check_port:
        return check_port(args.host, args.port)
    return wait_for_health(args.host, args.port, args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
