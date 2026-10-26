from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import random
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from app.compliance import local_posts_today
from app.config import ROOT, Settings, load_settings
from app.growth import load_engagement_state
from app.remote_state import pull_state, push_state, remote_enabled
from app.state import load_state

logger = logging.getLogger("scheduler")
STOP = threading.Event()


@dataclass
class Event:
    kind: Literal["post", "reply"]
    at: datetime
    done: bool = False
    attempts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "at": self.at.isoformat(),
            "done": self.done,
            "attempts": self.attempts,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Event":
        return cls(
            kind=str(payload.get("kind") or "post"),  # type: ignore[arg-type]
            at=datetime.fromisoformat(str(payload["at"])),
            done=bool(payload.get("done")),
            attempts=int(payload.get("attempts") or 0),
        )


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _clock(raw: str, default: time) -> time:
    try:
        hour, minute = raw.strip().split(":", 1)
        return time(int(hour), int(minute))
    except (ValueError, AttributeError):
        return default


def _random_times(
    day: date,
    *,
    timezone_name: str,
    count: int,
    start: time,
    end: time,
    min_gap_minutes: int,
    rng: random.Random,
    not_before: datetime | None = None,
) -> list[datetime]:
    if count <= 0:
        return []
    tz = ZoneInfo(timezone_name)
    start_at = datetime.combine(day, start, tzinfo=tz)
    if not_before is not None and not_before > start_at:
        start_at = not_before.astimezone(tz)
    end_at = datetime.combine(day, end, tzinfo=tz)
    span_minutes = (end_at - start_at).total_seconds() / 60
    usable = span_minutes - min_gap_minutes * (count - 1)
    if usable <= 0:
        return []
    chunk = usable / count
    points: list[datetime] = []
    for index in range(count):
        offset = index * (chunk + min_gap_minutes) + rng.uniform(0, chunk)
        point = start_at + timedelta(minutes=offset)
        points.append(point.replace(second=0, microsecond=0))
    return points


def _schedule_payload(
    settings: Settings,
    *,
    day: date,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    rng = rng or random.Random()
    start = _clock(os.getenv("SCHEDULE_START", "08:00"), time(8, 0))
    end = _clock(os.getenv("SCHEDULE_END", "23:30"), time(23, 30))
    target_posts = min(
        _env_int("SCHEDULE_POSTS_PER_DAY", 4),
        settings.max_posts_per_day,
        settings.max_total_posts_per_day,
    )
    target_replies = min(
        _env_int("SCHEDULE_REPLIES_PER_DAY", 30),
        settings.engagement_daily_limit,
    )
    history = load_state(settings.state_dir / "history.json")
    engagement = load_engagement_state(settings.state_dir / "engagement.json")
    tz = ZoneInfo(settings.timezone)
    day_key = day.isoformat()
    existing_posts = len(local_posts_today(history, now or datetime.now(timezone.utc), settings))
    existing_replies = 0
    for entry in engagement.get("replies", []):
        try:
            posted = datetime.fromisoformat(str(entry.get("posted_at") or ""))
        except ValueError:
            continue
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        if posted.astimezone(tz).date().isoformat() == day_key:
            existing_replies += 1
    post_count = max(0, target_posts - existing_posts)
    reply_count = max(0, target_replies - existing_replies)
    not_before = None
    if now is not None:
        local_now = now.astimezone(tz)
        if local_now.date() == day:
            not_before = local_now + timedelta(minutes=2)
    post_min_gap = _env_int("SCHEDULE_POST_MIN_GAP_MINUTES", 90)
    reply_min_gap = _env_int("SCHEDULE_REPLY_MIN_GAP_MINUTES", 12)
    events = [
        Event("post", at)
        for at in _random_times(
            day,
            timezone_name=settings.timezone,
            count=post_count,
            start=start,
            end=end,
            min_gap_minutes=post_min_gap,
            rng=rng,
            not_before=not_before,
        )
    ]
    events.extend(
        Event("reply", at)
        for at in _random_times(
            day,
            timezone_name=settings.timezone,
            count=reply_count,
            start=start,
            end=end,
            min_gap_minutes=reply_min_gap,
            rng=rng,
            not_before=not_before,
        )
    )
    events.sort(key=lambda event: event.at)
    return {
        "date": day.isoformat(),
        "timezone": settings.timezone,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "events": [event.to_dict() for event in events],
    }


def _load_schedule(path: Path, settings: Settings, now: datetime) -> dict[str, Any]:
    local_day = now.astimezone(ZoneInfo(settings.timezone)).date()
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("date") == local_day.isoformat():
                payload.setdefault("events", [])
                return payload
        except (OSError, json.JSONDecodeError):
            logger.warning("Ignoring unreadable schedule %s", path)
    payload = _schedule_payload(settings, day=local_day, now=now)
    _save_json(path, payload)
    logger.info("Created schedule for %s with %s events", local_day, len(payload["events"]))
    return payload


def _save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _events(payload: dict[str, Any]) -> list[Event]:
    return [Event.from_dict(item) for item in payload.get("events", [])]


def _set_events(payload: dict[str, Any], events: list[Event]) -> None:
    events.sort(key=lambda event: event.at)
    payload["events"] = [event.to_dict() for event in events]


def _slots(events: list[Event]) -> tuple[list[Event], list[Event]]:
    return (
        [event for event in events if event.kind == "post"],
        [event for event in events if event.kind == "reply"],
    )


def _print_schedule(payload: dict[str, Any], settings: Settings) -> None:
    tz = ZoneInfo(settings.timezone)
    events = _events(payload)
    posts, replies = _slots(events)
    print(f"Schedule {payload['date']} ({settings.timezone})")
    print(f"Posts: {len(posts)} | Replies: {len(replies)}")
    for event in events:
        local = event.at.astimezone(tz)
        marker = "done" if event.done else "pending"
        print(f"  {local.strftime('%H:%M')}  {event.kind:<5}  {marker}  attempts={event.attempts}")


def _run_event(settings: Settings, event: Event, *, dry_run: bool) -> dict[str, Any] | None:
    if event.kind == "post":
        args = ["--dry-run", "--json"] if dry_run else ["--live", "--ignore-window", "--json"]
    else:
        args = ["--engagement-plan", "--json"] if dry_run else ["--engagement-live", "--json"]
    command = [sys.executable, "-m", "app.main", *args]
    logger.info("Running %s event: %s", event.kind, " ".join(command))
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=1800,
        )
    except subprocess.TimeoutExpired:
        logger.error("%s event timed out", event.kind)
        return None
    if completed.stderr.strip():
        logger.info("command stderr: %s", completed.stderr.strip()[-2000:])
    output = completed.stdout.strip()
    if output:
        logger.info("command stdout: %s", output[-4000:])
    payload: dict[str, Any] | None = None
    if output:
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            start = output.find("{")
            end = output.rfind("}")
            if start >= 0 and end > start:
                try:
                    payload = json.loads(output[start : end + 1])
                except json.JSONDecodeError:
                    logger.warning("Command did not return JSON")
            else:
                logger.warning("Command did not return JSON")
    if completed.returncode != 0:
        logger.warning("%s event exited %s", event.kind, completed.returncode)
    return payload


def _retry_event(
    payload: dict[str, Any],
    event: Event,
    result: dict[str, Any] | None,
    *,
    now: datetime,
    settings: Settings,
) -> None:
    if event.kind != "reply" or event.attempts >= 2 or not result:
        return
    status = str(result.get("status") or "")
    if status not in {"no_target", "no_comment", "skipped", "ai_required"}:
        return
    local = now.astimezone(ZoneInfo(settings.timezone))
    end = _clock(os.getenv("SCHEDULE_END", "23:30"), time(23, 30))
    if local.time() >= end:
        return
    delay = random.randint(15, 35)
    retry_at = now + timedelta(minutes=delay)
    events = _events(payload)
    if any(not item.done and item.kind == "reply" and abs((item.at - retry_at).total_seconds()) < 60 for item in events):
        return
    retry = Event("reply", retry_at, attempts=event.attempts + 1)
    events.append(retry)
    _set_events(payload, events)
    logger.info("Queued reply retry at %s", retry_at.astimezone(ZoneInfo(settings.timezone)).strftime("%H:%M"))


def _sleep_until(target: datetime, stop: threading.Event) -> None:
    while not stop.is_set():
        remaining = (target - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            return
        stop.wait(min(remaining, 30))


def _run_scheduler(settings: Settings, *, dry_run: bool) -> int:
    tz = ZoneInfo(settings.timezone)
    state_path = settings.state_dir / "scheduler.json"
    lock_path = settings.state_dir / "scheduler.lock"
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.error("Another scheduler instance is already running")
        return 1

    def stop(_signum: int, _frame: Any) -> None:
        logger.info("Stopping scheduler")
        STOP.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    remote_requested = remote_enabled()
    remote_ready = pull_state(settings) if remote_requested else False
    if remote_requested and not remote_ready:
        logger.error("Remote state is enabled but unavailable; refusing to start")
        return 1

    logger.info(
        "Scheduler started in %s; posts=%s replies=%s",
        settings.timezone,
        os.getenv("SCHEDULE_POSTS_PER_DAY", "4"),
        os.getenv("SCHEDULE_REPLIES_PER_DAY", "30"),
    )
    last_schedule_date = ""
    state_dirty = False
    while not STOP.is_set():
        now = datetime.now(timezone.utc)
        payload = _load_schedule(state_path, settings, now)
        if remote_ready and payload.get("date") != last_schedule_date:
            if push_state(settings, "chore: save daily schedule"):
                last_schedule_date = str(payload.get("date") or "")
        if remote_ready and state_dirty and push_state(settings, "chore: retry state sync"):
            state_dirty = False
        events = _events(payload)
        pending = [event for event in events if not event.done]
        if not pending:
            local = now.astimezone(tz)
            tomorrow = datetime.combine(
                local.date() + timedelta(days=1),
                time(0, 5),
                tzinfo=tz,
            )
            logger.info("Schedule complete; next schedule at %s", tomorrow.strftime("%Y-%m-%d %H:%M"))
            _sleep_until(tomorrow, STOP)
            continue

        next_event = min(pending, key=lambda event: event.at)
        if next_event.at <= now + timedelta(seconds=5):
            for event in events:
                if not event.done and event.at < now - timedelta(minutes=10):
                    event.done = True
                    logger.info("Skipping missed %s slot at %s", event.kind, event.at)
            _set_events(payload, events)
            _save_json(state_path, payload)
            if remote_ready:
                state_dirty = not push_state(settings, "chore: skip missed bot slots")
            pending = [event for event in events if not event.done]
            if not pending:
                continue
            next_event = min(pending, key=lambda event: event.at)
            if next_event.at <= now + timedelta(seconds=5):
                result = _run_event(settings, next_event, dry_run=dry_run)
                next_event.done = True
                _retry_event(payload, next_event, result, now=now, settings=settings)
                _set_events(payload, events)
                _save_json(state_path, payload)
                if remote_ready:
                    state_dirty = not push_state(
                        settings, f"chore: record {next_event.kind} event"
                    )
                continue

        logger.info(
            "Next %s at %s",
            next_event.kind,
            next_event.at.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
        )
        _sleep_until(next_event.at, STOP)

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run randomized Bluesky posting and reply slots.")
    parser.add_argument("--dry-run", action="store_true", help="Use dry-run/plan commands")
    parser.add_argument("--print-schedule", action="store_true", help="Print today's schedule and exit")
    parser.add_argument("--reset-schedule", action="store_true", help="Regenerate today's schedule")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = load_settings()
    if args.reset_schedule:
        path = settings.state_dir / "scheduler.json"
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    if args.print_schedule:
        payload = _load_schedule(settings.state_dir / "scheduler.json", settings, datetime.now(timezone.utc))
        _print_schedule(payload, settings)
        return 0
    return _run_scheduler(settings, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
