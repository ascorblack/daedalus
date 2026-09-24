"""A stand-in for the terminal daemon's ``hook-post`` command: ``hook-post <name> [--wait-ms N]``.

It reads a hook payload on stdin, posts it to ``$DAEDALUS_HOOK_URL/<name>`` with the launch's token,
prints the reply body, and exits 0 on a 2xx, 2 when the launch is unknown or the token wrong (401,
410), and 1 otherwise — including when nothing answers, so a dead host never blocks a CLI's hook for
longer than its timeout.
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    args = sys.argv[1:]
    if not args:
        sys.stderr.write("usage: hook-post <name> [--wait-ms N]\n")
        return 1
    name, wait = args[0], 0
    if len(args) >= 3 and args[1] == "--wait-ms":
        wait = int(args[2])
    url = os.environ.get("DAEDALUS_HOOK_URL", "").rstrip("/") + f"/{name}" + (f"?wait_ms={wait}" if wait else "")
    request = urllib.request.Request(url, data=sys.stdin.buffer.read(), method="POST", headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + os.environ.get("DAEDALUS_HOOK_TOKEN", ""),
    })
    try:
        with urllib.request.urlopen(request, timeout=wait / 1000 + 10) as response:
            sys.stdout.buffer.write(response.read())
            return 0
    except urllib.error.HTTPError as exc:
        return 2 if exc.code in (401, 410) else 1
    except (urllib.error.URLError, OSError, TimeoutError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
