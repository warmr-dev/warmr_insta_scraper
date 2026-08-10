"""Read the tray and list the stories currently visible to a worker account.

    python scripts/check_stories.py                       # use the saved session
    python scripts/check_stories.py --sessionid '<id>'    # use a browser session

Read-only. It never calls `media/seen/` or any write endpoint (SPEC section 11),
so viewing here does NOT put the worker account in anyone's viewer list.

Why it avoids `feed/reels_media/`: that endpoint is mobile-app-scoped. Calling it
on a browser-derived session returns 403 AND gets the session revoked - we lost a
session that way. Story items are taken from the tray's own prefetched `items`
instead, which is the fast path the poller already prefers (SPEC 7.1). There is
deliberately NO per-account fallback: looping `user_stories` over targets is the
design mistake SPEC section 1 and 11 forbid.
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from stories_monitor.db.models import WorkerAccount  # noqa: E402
from stories_monitor.db.session import session_scope  # noqa: E402
from stories_monitor.logging_setup import configure_logging  # noqa: E402
from stories_monitor.transport.live import LiveTransport  # noqa: E402
from stories_monitor.workers.fetcher import _story_item_from_payload  # noqa: E402


def _age(ts: int, now: dt.datetime) -> str:
    taken = dt.datetime.fromtimestamp(ts, tz=dt.UTC)
    hours = (now - taken).total_seconds() / 3600
    return f"{taken:%H:%M UTC} ({hours:.1f}h ago)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--username", default=None, help="Worker account (default: first usable)")
    ap.add_argument("--sessionid", default=None, help="Browser sessionid to use instead")
    args = ap.parse_args()
    configure_logging(json_output=False)

    with session_scope() as s:
        q = s.query(WorkerAccount)
        if args.username:
            q = q.filter_by(username=args.username)
        account = q.filter(WorkerAccount.session_json.isnot(None)).first() or q.first()
        if account is None:
            print("no worker account seeded - run `stories seed-worker` first")
            return 2
        username = account.username
        device = dict(account.device_settings or {})
        saved = dict(account.session_json or {})
        proxy = account.proxy_url or None

    t = LiveTransport(username=username, device_settings=device, proxy_url=proxy)

    if args.sessionid:
        t.client.login_by_sessionid(args.sessionid)
        print(f"using supplied browser session for @{username}")
    elif saved:
        t.load_session(saved)
        print(f"restored saved session for @{username}")
    else:
        print(f"@{username} has no saved session - run scripts/login_now.py first")
        return 2

    try:
        tray = t.reels_tray(cold_start=True)
    except Exception as exc:  # noqa: BLE001
        print(f"reels_tray failed: {type(exc).__name__}: {exc}")
        return 1

    users = [e for e in tray.entries if e.is_user_entry]
    highlights = tray.entry_count - len(users)
    print(
        f"\ntray: {tray.entry_count} entries | {len(users)} accounts with live stories "
        f"| {highlights} highlights skipped | truncated={bool(tray.next_max_id)}\n"
    )

    now = dt.datetime.now(dt.UTC)
    photos = videos = 0

    for entry in users:
        name = entry.user.get("username", entry.id)
        items = []
        source = "tray-prefetch"

        if entry.has_prefetched_items:
            items = [
                item
                for raw in entry.items
                if (item := _story_item_from_payload(entry.user_id, raw)) is not None
            ]
        if not items:
            print(f"@{name:20} (no item detail; latest_reel_media={entry.latest_reel_media})")
            continue

        print(f"@{name:20} {len(items)} stories  [{source}]")
        for item in items:
            kind = "PHOTO" if item.is_photo else "video"
            if item.is_photo:
                photos += 1
            else:
                videos += 1
            print(f"     [{kind}] {item.story_id}  {_age(item.taken_at, now)}")

    print(f"\nTOTAL: {photos} photos -> AI pipeline | {videos} videos -> skipped (SPEC 1)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
