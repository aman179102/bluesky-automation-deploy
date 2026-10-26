from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from app import compliance
from app.bluesky import BlueskyClient, build_facets, make_tid
from app.config import ConfigError, Settings, load_settings
from app.growth import (
    build_growth_report,
    load_engagement_state,
    load_growth_state,
    plan_engagement,
    run_engagement,
    save_engagement_state,
    save_growth_state,
)
from app.image import render_image
from app.models import PostDraft, Story
from app.select import rank_candidates, select_story
from app.sources import enrich_story, fetch_all
from app.state import (
    load_state,
    mark_seen,
    prune_state,
    record_post,
    save_state,
)
from app.writer import Writer, WriterError

logger = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Post one crisp, source-linked tech news item to Bluesky."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="Publish to Bluesky")
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Build the post and image without publishing (default)",
    )
    parser.add_argument(
        "--setup-account",
        action="store_true",
        help="Add the Bluesky self-label 'bot' to the profile",
    )
    parser.add_argument(
        "--check-account",
        action="store_true",
        help="Print account and bot-label status",
    )
    parser.add_argument(
        "--remove-bot-label",
        action="store_true",
        help="Remove the Bluesky self-label 'bot' from the profile",
    )
    parser.add_argument(
        "--delete-uri",
        help="Delete one post by at:// URI (manual correction)",
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Show ranked story candidates and virality reasons without posting",
    )
    parser.add_argument(
        "--growth-report",
        action="store_true",
        help="Print follower and per-post engagement metrics",
    )
    parser.add_argument(
        "--engagement-plan",
        action="store_true",
        help="Find relevant posts and generate human-reviewable reply drafts",
    )
    parser.add_argument(
        "--engagement-live",
        action="store_true",
        help="Post one auto-generated relevant reply (strictly capped)",
    )
    parser.add_argument("--force", action="store_true", help="Bypass posting windows and caps")
    parser.add_argument(
        "--ignore-window",
        action="store_true",
        help="Allow posting outside the configured windows while keeping daily caps",
    )
    parser.add_argument("--no-ai", action="store_true", help="Use the template writer")
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--json", action="store_true", help="Print a JSON result")
    return parser


def _settings(args: argparse.Namespace) -> Settings:
    settings = load_settings()
    updates = {}
    if args.state_dir:
        updates["state_dir"] = args.state_dir
    if args.output_dir:
        updates["output_dir"] = args.output_dir
    return replace(settings, **updates) if updates else settings


def _print_result(payload: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    status = payload.get("status", "unknown")
    print(f"Status: {status}")
    for key in ("title", "category", "provider", "image", "reason"):
        if payload.get(key):
            print(f"{key}: {payload[key]}")
    if payload.get("text"):
        print("\n" + payload["text"] + "\n")
    if payload.get("sources"):
        print("Sources:")
        for source in payload["sources"]:
            print(f"  - {source.get('name')}: {source.get('url')}")
    if payload.get("errors"):
        print("Errors:")
        for error in payload["errors"]:
            print(f"  - {error}")


def _rkey(now: datetime, history: dict, settings: Settings) -> str:
    local = now.astimezone(ZoneInfo(settings.timezone))
    hour = local.hour
    if hour < 12:
        clock_id = 1
    elif hour < 17:
        clock_id = 2
    elif hour < 21:
        clock_id = 3
    else:
        clock_id = 4
    existing = {str(post.get("rkey", "")) for post in history.get("posts", [])}
    for offset in range(1024):
        candidate = make_tid(now, (clock_id + offset) % 1024)
        if candidate not in existing:
            return candidate
    raise RuntimeError("could not allocate a unique post rkey")


def _source_payload(draft: PostDraft) -> list[dict[str, str]]:
    return [
        {"name": ref.name, "url": ref.url, "kind": ref.kind}
        for ref in draft.source_refs
    ]


def _validate_draft(
    draft: PostDraft, story: Story, settings: Settings
) -> list[str]:
    errors = compliance.validate_text(draft, story, settings)
    if not build_facets(draft.text, draft.source_refs):
        errors.append("source line is not clickable (no link facet)")
    return errors


def _draft_with_fallback(
    story: Story, settings: Settings, no_ai: bool
) -> tuple[PostDraft | None, list[str]]:
    attempts: list[list[str]] = []
    draft = Writer(settings, allow_ai=not no_ai).draft(story)
    errors = _validate_draft(draft, story, settings)
    attempts.append(errors)
    if errors and draft.provider != "template":
        logger.warning("AI draft failed compliance; trying template fallback")
        draft = Writer(settings, allow_ai=False).draft(story)
        errors = _validate_draft(draft, story, settings)
        attempts.append(errors)
    if errors:
        return None, [error for group in attempts for error in group]
    return draft, []


def _check_source_links(draft: PostDraft, settings: Settings) -> list[str]:
    errors: list[str] = []
    if not draft.source_refs:
        return ["no source links"]
    ok, reason = compliance.validate_source_url(draft.source_refs[0].url, settings)
    if not ok:
        errors.append(f"primary source link: {reason}")
    return errors


def _ping_healthcheck(settings: Settings) -> None:
    if not settings.healthcheck_url:
        return
    try:
        httpx.get(settings.healthcheck_url, timeout=10)
    except httpx.HTTPError:
        logger.warning("Healthcheck ping failed")


def _setup_account(settings: Settings, as_json: bool) -> int:
    settings.require_bluesky()
    with BlueskyClient(settings) as client:
        client.login()
        already = client.has_bot_label()
        if not already:
            client.set_bot_label()
        _print_result(
            {
                "status": "bot_label_present" if already else "bot_label_added",
                "handle": client.handle,
                "did": client.did,
            },
            as_json,
        )
    return 0


def _remove_bot_label(settings: Settings, as_json: bool) -> int:
    settings.require_bluesky()
    with BlueskyClient(settings) as client:
        client.login()
        result = client.remove_bot_label()
        _print_result(
            {
                "status": result.get("status", "bot_label_removed"),
                "handle": client.handle,
                "did": client.did,
            },
            as_json,
        )
    return 0


def _check_account(settings: Settings, as_json: bool) -> int:
    settings.require_bluesky()
    with BlueskyClient(settings) as client:
        client.login()
        record = client.get_profile_record() or {}
        _print_result(
            {
                "status": "account_ok",
                "handle": client.handle,
                "did": client.did,
                "bot_label": client.has_bot_label(),
                "display_name": record.get("displayName", ""),
                "description": record.get("description", ""),
            },
            as_json,
        )
    return 0


def _delete_post(settings: Settings, uri: str, as_json: bool) -> int:
    settings.require_bluesky()
    match = re.match(r"at://([^/]+)/app\.bsky\.feed\.post/([^/]+)$", uri.strip())
    if not match:
        raise ConfigError("--delete-uri must look like at://did/app.bsky.feed.post/rkey")
    with BlueskyClient(settings) as client:
        client.login()
        if match.group(1) != client.did:
            raise ConfigError("The post URI belongs to a different account")
        client.delete_post(match.group(2))
    _print_result({"status": "deleted", "uri": uri}, as_json)
    return 0


def _run_plan(settings: Settings, as_json: bool) -> int:
    state_path = settings.state_dir / "history.json"
    state = load_state(state_path)
    now = datetime.now(timezone.utc)
    prune_state(state, now, settings.history_days)
    items, errors = fetch_all(settings)
    candidates = rank_candidates(items, state, now, settings)
    eligible = [candidate for candidate in candidates if candidate.eligible]
    payload = {
        "status": "plan",
        "eligible": len(eligible),
        "total": len(candidates),
        "candidates": [candidate.to_dict() for candidate in candidates[:15]],
        "errors": errors,
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"Status: plan ({len(eligible)} ready / {len(candidates)} checked)")
    if not candidates:
        print("No candidates returned.")
        return 0
    for index, candidate in enumerate(candidates[:15], start=1):
        flag = "READY" if candidate.eligible else "SKIP "
        virality = candidate.virality
        print(
            f"{index:>2}. {flag} {candidate.score:5.2f}  "
            f"viral {virality.score:>3} ({virality.audience})  "
            f"[{candidate.item.category}] {candidate.item.title}"
        )
        print(f"    {candidate.item.source} | {candidate.item.url}")
        if candidate.reason:
            print(f"    reason: {candidate.reason}")
        elif virality.reasons:
            print("    signals: " + "; ".join(virality.reasons))
    if errors:
        print("Source warnings:")
        for error in errors:
            print(f"  - {error}")
    return 0


def _run_engagement(settings: Settings, as_json: bool, live: bool) -> int:
    settings.require_bluesky()
    state_path = settings.state_dir / "engagement.json"
    state = load_engagement_state(state_path)
    now = datetime.now(timezone.utc)
    with BlueskyClient(settings) as client:
        client.login()
        if live:
            result = run_engagement(client, settings, state, now=now)
            if result.get("status") == "commented":
                save_engagement_state(state_path, state)
        else:
            result = plan_engagement(client, settings, state, now=now, limit=5)

    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    status = result.get("status", "unknown")
    print(f"Status: {status}")
    if result.get("reason"):
        print(f"Reason: {result['reason']}")
    if status in {"engagement_plan", "ai_required"}:
        print(f"Targets: {result.get('count', result.get('targets', []).__len__())}")
        for index, target in enumerate(result.get("targets", []), start=1):
            print(
                f"{index}. @{target.get('author')} | "
                f"{target.get('likes')} likes, {target.get('reposts')} reposts, "
                f"{target.get('replies')} replies"
            )
            print(f"   {target.get('url')}")
            print(f"   {str(target.get('text', ''))[:220]}")
        for suggestion in result.get("suggestions", []):
            print(f"\nSuggested reply -> {suggestion.get('url')}")
            print(f"  {suggestion.get('suggested_reply')}")
        for skipped in result.get("skipped", []):
            print(f"\nSkipped {skipped.get('uri')}: {skipped.get('reason')}")
    elif status == "commented":
        last = result.get("last_reply", {})
        print(f"Reply: {last.get('reply_text', '')}")
        print(f"To: {last.get('author', '')}")
        print(f"URI: {last.get('reply_uri', '')}")
    elif result.get("skipped"):
        print("Skipped:")
        for skipped in result["skipped"]:
            print(f"  - {skipped.get('uri')}: {skipped.get('reason')}")
    return 0


def _run_growth(settings: Settings, as_json: bool) -> int:
    settings.require_bluesky()
    state_path = settings.state_dir / "growth.json"
    state = load_growth_state(state_path)
    now = datetime.now(timezone.utc)
    with BlueskyClient(settings) as client:
        client.login()
        report = build_growth_report(
            client, client.handle or settings.handle, previous=state, now=now
        )
    state.setdefault("snapshots", []).append(report["snapshot"])
    save_growth_state(state_path, state)

    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    delta = report.get("followers_delta", 0)
    print(f"Status: growth_report for @{report.get('handle')}")
    print(
        f"Followers: {report.get('followers')} ({delta:+d} since last snapshot) | "
        f"Following: {report.get('following')} | Posts: {report.get('posts')}"
    )
    print(
        f"Analyzed {report.get('analyzed_posts')} posts | "
        f"avg engagement {report.get('average_engagement')} | "
        f"engagement rate {report.get('engagement_rate_pct')}%"
    )
    print("Top posts:")
    for index, post in enumerate(report.get("top_posts", []), start=1):
        print(
            f"{index}. {post.get('likes')} likes / {post.get('reposts')} reposts / "
            f"{post.get('replies')} replies | {post.get('url')}"
        )
        print(f"   {str(post.get('text', ''))[:180]}")
    return 0


def _run_post(args: argparse.Namespace, settings: Settings) -> int:
    if args.live and not settings.posting_enabled:
        _print_result(
            {
                "status": "disabled",
                "reason": "POSTING_ENABLED is false; live posting is disabled",
            },
            args.json,
        )
        return 0

    state_path = settings.state_dir / "history.json"
    state = load_state(state_path)
    now = datetime.now(timezone.utc)
    prune_state(state, now, settings.history_days)

    items, errors = fetch_all(settings)
    for error in errors:
        logger.warning("source: %s", error)
    logger.info("Fetched %s eligible items", len(items))
    if not items:
        payload = {"status": "no_sources", "errors": errors}
        _print_result(payload, args.json)
        return 1 if errors else 0

    story = select_story(items, state, now, settings)
    if not story:
        _print_result({"status": "no_story", "errors": errors}, args.json)
        return 0

    with httpx.Client(
        headers={"User-Agent": settings.user_agent},
        timeout=settings.request_timeout,
        follow_redirects=True,
    ) as client:
        story = enrich_story(story, client, settings)

    if not args.force:
        allowed, reason = compliance.can_post_now(
            state,
            now,
            story.is_breaking,
            settings,
            ignore_window=args.ignore_window,
        )
        if not allowed:
            _print_result(
                {
                    "status": "skipped",
                    "reason": reason,
                    "title": story.title,
                    "category": story.category,
                    "breaking": story.is_breaking,
                },
                args.json,
            )
            return 0

    draft, draft_errors = _draft_with_fallback(story, settings, args.no_ai)
    if not draft:
        _print_result(
            {
                "status": "invalid_draft",
                "title": story.title,
                "errors": draft_errors,
            },
            args.json,
        )
        return 1

    link_errors = _check_source_links(draft, settings)
    if link_errors:
        _print_result(
            {
                "status": "invalid_source",
                "title": story.title,
                "errors": link_errors,
            },
            args.json,
        )
        return 1

    rkey = _rkey(now, state, settings)
    image_path = None
    image_errors = []
    if settings.post_with_image:
        image_path = settings.output_dir / f"{rkey}.jpg"
        render_image(draft, story, image_path, handle=settings.handle or "aman179102.bsky.social")
        image_errors = compliance.validate_image(image_path)
        if image_errors:
            _print_result(
                {
                    "status": "invalid_image",
                    "title": story.title,
                    "errors": image_errors,
                },
                args.json,
            )
            return 1

    facets = build_facets(draft.text, draft.source_refs)
    payload = {
        "status": "dry_run" if not args.live else "posting",
        "title": story.title,
        "category": story.category,
        "provider": draft.provider,
        "rkey": rkey,
        "text": draft.text,
        "image": str(image_path) if image_path else "",
        "alt_text": draft.alt_text if image_path else "",
        "sources": _source_payload(draft),
        "facets": len(facets),
        "breaking": story.is_breaking,
        "errors": errors,
    }

    if not args.live:
        _print_result(payload, args.json)
        return 0

    settings.require_posting_enabled()
    settings.require_bluesky()
    with BlueskyClient(settings) as client:
        client.login()
        result = client.create_post(
            text=draft.text,
            facets=facets,
            image_path=image_path,
            alt_text=draft.alt_text if image_path else "",
            rkey=rkey,
            created_at=now,
        )

    mark_seen(state, story.item, now)
    state["last_run"] = now.isoformat()
    record_post(
        state,
        uri=result.get("uri", ""),
        cid=result.get("cid", ""),
        rkey=rkey,
        text=draft.text,
        title=story.title,
        url=story.primary_source.url,
        category=story.category,
        source_names=[ref.name for ref in draft.source_refs],
        posted_at=now,
        breaking=story.is_breaking,
    )
    save_state(state_path, state)
    _ping_healthcheck(settings)
    payload["status"] = "posted"
    payload["uri"] = result.get("uri", "")
    payload["cid"] = result.get("cid", "")
    _print_result(payload, args.json)
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parser().parse_args(argv)
    try:
        settings = _settings(args)
        if args.setup_account:
            return _setup_account(settings, args.json)
        if args.check_account:
            return _check_account(settings, args.json)
        if args.remove_bot_label:
            return _remove_bot_label(settings, args.json)
        if args.delete_uri:
            return _delete_post(settings, args.delete_uri, args.json)
        if args.plan:
            return _run_plan(settings, args.json)
        if args.growth_report:
            return _run_growth(settings, args.json)
        if args.engagement_plan or args.engagement_live:
            return _run_engagement(settings, args.json, args.engagement_live)
        return _run_post(args, settings)
    except (ConfigError, WriterError, compliance.ComplianceError, httpx.HTTPError) as exc:
        logger.error("%s", exc)
        if args.json:
            print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 1
    except Exception as exc:  # pragma: no cover - last-resort safety
        logger.exception("Unexpected failure: %s", exc)
        if args.json:
            print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
