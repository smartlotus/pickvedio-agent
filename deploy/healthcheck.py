"""Container health check for the pickvedio-core service.

Used by the Dockerfile HEALTHCHECK, and importable/runnable by any host that
only lets you specify a command (Railway "Healthcheck Path" hits /healthz
itself, so this exists mainly for plain Docker and for local debugging).

Why Python instead of curl: the slim image has no curl, and installing one just
to answer a health probe would add ~15 MB to every image.

Exit code 0 = healthy. Anything else = unhealthy, and the platform restarts or
routes away according to its own policy.

`/healthz` is deliberately the only route probed: it requires no bearer token,
so a health check can never leak PVC_API_TOKEN into a platform's command log.
"""
from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    port = os.environ.get("PORT", "8080").strip() or "8080"
    url = f"http://127.0.0.1:{port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=4) as response:
            body = response.read(256).decode("utf-8", "replace")
            if response.status == 200:
                return 0
            print(f"unhealthy: {url} -> HTTP {response.status} {body!r}", file=sys.stderr)
            return 1
    except urllib.error.HTTPError as exc:  # non-2xx
        print(f"unhealthy: {url} -> HTTP {exc.code}", file=sys.stderr)
        return 1
    except Exception as exc:  # connection refused, timeout, ...
        print(f"unhealthy: {url} -> {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
