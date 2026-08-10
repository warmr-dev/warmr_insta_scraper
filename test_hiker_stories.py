"""Pilot test for HikerAPI Stories endpoint.

The script reads up to 500 Instagram user IDs from the project CSV, requests
their current stories, and saves only non-media metadata as JSONL.

Required environment variable:
    HIKER_API_KEY

Example in PowerShell:
    $env:HIKER_API_KEY = "your-key"
    python .\test_hiker_stories.py
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_CSV = (
    Path(__file__).parent
    / "estimation"
    / "combined_deduped_final.xlsx - Sheet1.csv"
)
API_URL = "https://api.hikerapi.com/v1/user/stories"


def load_api_key() -> str | None:
    """Read the key from the process environment or the local .env file."""
    key = os.environ.get("HIKER_API_KEY")
    if key:
        return key.strip().strip("\"'")

    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return None

    with env_path.open("r", encoding="utf-8") as env_file:
        for line in env_file:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            name, value = stripped.split("=", 1)
            if name.strip() == "HIKER_API_KEY":
                return value.strip().strip("\"'")
    return None


def load_user_ids(csv_path: Path, limit: int) -> list[str]:
    """Load unique numeric Instagram IDs from the CSV."""
    user_ids: list[str] = []
    seen: set[str] = set()

    with csv_path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        for row in reader:
            raw_id = (row.get("id") or row.get("pk_id") or row.get("pk") or "").strip()
            if not raw_id or not raw_id.isdigit() or raw_id in seen:
                continue
            seen.add(raw_id)
            user_ids.append(raw_id)
            if len(user_ids) >= limit:
                break

    return user_ids


def request_stories(
    user_id: str,
    api_key: str,
    force: bool,
    timeout: float,
    retries: int,
) -> dict[str, Any]:
    """Request stories for one user and return sanitized metadata."""
    query = {"user_id": user_id}
    if force:
        query["force"] = "on"

    request = Request(
        f"{API_URL}?{urlencode(query)}",
        headers={
            "x-access-key": api_key,
            "Accept": "application/json",
            "User-Agent": "warmr-hiker-pilot/1.0",
        },
        method="GET",
    )

    started = time.perf_counter()
    last_error = ""

    for attempt in range(retries + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
                request_units = parse_request_units(response.headers.get("x-hiker-info"))
                elapsed_ms = round((time.perf_counter() - started) * 1000)
                stories = payload if isinstance(payload, list) else payload.get("stories", [])
                return {
                    "user_id": user_id,
                    "ok": True,
                    "http_status": response.status,
                    "request_units": request_units,
                    "elapsed_ms": elapsed_ms,
                    "story_count": len(stories) if isinstance(stories, list) else 0,
                    "stories": sanitize_stories(stories),
                }
        except HTTPError as error:
            response_body = error.read().decode("utf-8", errors="replace")
            last_error = f"HTTP {error.code}: {response_body[:300]}"
            if error.code not in {408, 425, 429, 500, 502, 503, 504}:
                break
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            last_error = str(error)

        if attempt < retries:
            time.sleep(2**attempt)

    elapsed_ms = round((time.perf_counter() - started) * 1000)
    return {
        "user_id": user_id,
        "ok": False,
        "http_status": None,
        "request_units": 0,
        "elapsed_ms": elapsed_ms,
        "story_count": 0,
        "stories": [],
        "error": last_error,
    }


def parse_request_units(header_value: str | None) -> int:
    if not header_value:
        return 0
    try:
        parsed = json.loads(header_value)
        return int(parsed.get("reqs", 0))
    except (TypeError, ValueError, json.JSONDecodeError):
        return 0


def sanitize_stories(stories: Any) -> list[dict[str, Any]]:
    """Keep metadata only; never write media URLs or binary content."""
    if not isinstance(stories, list):
        return []

    sanitized: list[dict[str, Any]] = []
    for story in stories:
        if not isinstance(story, dict):
            continue
        media_type = story.get("media_type")
        sanitized.append(
            {
                "story_id": story.get("id") or story.get("pk"),
                "taken_at": story.get("taken_at"),
                "media_type": media_type,
                "media_kind": (
                    "photo"
                    if media_type == 1
                    else "video"
                    if media_type == 2
                    else "unknown"
                ),
                "product_type": story.get("product_type"),
            }
        )
    return sanitized


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a 500-account HikerAPI Stories pilot.")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument(
        "--no-force",
        action="store_true",
        help="Do not use force=on; this costs more request units.",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    api_key = load_api_key()
    if not api_key:
        print("ERROR: HIKER_API_KEY is not set.", file=sys.stderr)
        return 2
    if not args.csv.exists():
        print(f"ERROR: CSV file not found: {args.csv}", file=sys.stderr)
        return 2
    if not 1 <= args.limit <= 500:
        print("ERROR: --limit must be between 1 and 500.", file=sys.stderr)
        return 2
    if not 1 <= args.workers <= 20:
        print("ERROR: --workers must be between 1 and 20.", file=sys.stderr)
        return 2

    user_ids = load_user_ids(args.csv, args.limit)
    if not user_ids:
        print("ERROR: No numeric user IDs found in the CSV.", file=sys.stderr)
        return 2

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = args.output or Path(f"hiker_stories_pilot_{timestamp}.jsonl")
    results: list[dict[str, Any]] = []

    print(f"Testing {len(user_ids)} accounts with {args.workers} workers...")
    print(f"force= {'off' if args.no_force else 'on'}")
    started = time.perf_counter()

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                request_stories,
                user_id,
                api_key,
                not args.no_force,
                args.timeout,
                args.retries,
            ): user_id
            for user_id in user_ids
        }
        for index, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            results.append(result)
            if index % 25 == 0 or index == len(user_ids):
                print(f"Completed {index}/{len(user_ids)}")

    results.sort(key=lambda item: user_ids.index(item["user_id"]))
    with output_path.open("w", encoding="utf-8") as destination:
        for result in results:
            destination.write(json.dumps(result, ensure_ascii=False) + "\n")

    successful = [item for item in results if item["ok"]]
    failed = [item for item in results if not item["ok"]]
    stories = [story for item in successful for story in item["stories"]]
    photos = [story for story in stories if story["media_kind"] == "photo"]
    videos = [story for story in stories if story["media_kind"] == "video"]
    units = sum(item["request_units"] for item in results)
    elapsed = time.perf_counter() - started

    print("\nPilot summary")
    print(f"Accounts:       {len(results)}")
    print(f"Successful:     {len(successful)}")
    print(f"Failed:         {len(failed)}")
    print(f"Stories:        {len(stories)}")
    print(f"Photos:         {len(photos)}")
    print(f"Videos:         {len(videos)}")
    print(f"Request units:  {units}")
    print(f"Elapsed:        {elapsed:.1f}s")
    print(f"Results saved:  {output_path}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
