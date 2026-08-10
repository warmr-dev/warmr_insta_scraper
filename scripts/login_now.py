"""Interactive 2FA login for a worker account.

Run this yourself in a terminal:

    python scripts/login_now.py

Everything slow - the database read, transport construction, proxy binding - is
done BEFORE you are asked for a code, so the ~30s TOTP window is spent on the
login request itself rather than on setup. Type the code the moment it appears
in your authenticator app.

It performs exactly ONE login and saves the session, because repeated logins are
the single strongest ban signal (SPEC section 8). If a session already exists it
is reused and no login happens at all.
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from stories_monitor.config import get_settings  # noqa: E402
from stories_monitor.logging_setup import configure_logging  # noqa: E402


def ask_for_code() -> str:
    print()
    print("=" * 58)
    print("  Everything is ready. Open your authenticator app NOW.")
    print("  The code is sent immediately - no setup happens after this.")
    print("=" * 58)
    return input("  6-digit code: ").strip()


def main() -> int:
    configure_logging(json_output=False)
    settings = get_settings()

    if settings.ig_transport != "live":
        print(
            f"IG_TRANSPORT is '{settings.ig_transport}'. Set IG_TRANSPORT=live in "
            ".env for a real login, then re-run."
        )
        return 2

    from stories_monitor.smoke import run_login_test

    print(f"Account : {settings.ig_worker_username}")
    print("Preparing transport and proxy ...")

    result = run_login_test(
        settings.ig_worker_username or None, code_prompt=ask_for_code,
        allow_no_proxy=True
    )

    print()
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("logged_in") or result.get("result") == "session_reused" else 1


if __name__ == "__main__":
    raise SystemExit(main())
