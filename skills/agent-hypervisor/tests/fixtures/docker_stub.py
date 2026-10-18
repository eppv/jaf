#!/usr/bin/env python3
"""Stand-in for the docker CLI used by the isolation tests.

It records every invocation as one JSON array per line in
``$DOCKER_ARGS_LOG`` and pretends to be a daemon for ``docker info``. A
``docker run ... <image> sh -lc <cmd>`` call executes ``<cmd>`` on the host, so
the test can assert the argv shape without a real container.
"""

import json
import os
import subprocess
import sys


def main() -> int:
    args = sys.argv[1:]
    log = os.environ.get("DOCKER_ARGS_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(args) + "\n")
    if args and args[0] == "run":
        subprocess.run(["sh", "-lc", args[-1]])
    elif args and args[0] == "info":
        print("stub")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
