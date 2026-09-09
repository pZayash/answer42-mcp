#!/usr/bin/env python3
"""Read 1C web-client runtime metadata using requests only.

Uses the same temporary direct-login probe as Answer42. The probe always sends
``e1cib/logout`` after a successful ``startInfo`` request, so it does not leave
a server-side 1C web session waiting for its idle timeout.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys

from mcp_1c.webclient_probe import WebRuntimeAuthenticationError, WebRuntimeProbeError, probe_web_runtime


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url")
    parser.add_argument("--username-env", default="ONEC_USERNAME")
    parser.add_argument("--password-env", default="ONEC_PASSWORD")
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()
    username = os.getenv(args.username_env) or input("1C username: ")
    password = os.getenv(args.password_env) or getpass.getpass("1C password: ")
    try:
        print(json.dumps(probe_web_runtime(args.base_url, username, password, args.timeout), ensure_ascii=False, indent=2))
    except WebRuntimeAuthenticationError as exc:
        print(f"authentication failed: {exc}", file=sys.stderr)
        return 2
    except (WebRuntimeProbeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
