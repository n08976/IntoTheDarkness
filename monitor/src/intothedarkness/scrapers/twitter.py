"""Read a few X (Twitter) timelines as a logged-in session.

X has no free API, and the no-auth paths (syndication endpoints, Nitter, RSS
bridges) are empty or rate-limited in practice. The web client itself, though,
still fetches timelines over plain HTTP from the GraphQL endpoints once it has a
session cookie. So this scraper replays one burner account's cookies
(``auth_token`` + ``ct0``, local .env only) and calls the same two operations
the browser does -- UserByScreenName to resolve a handle to its id, then
UserTweets for the timeline -- with no headless browser.

X is clearnet, so every request goes direct (the routing rule forbids Tor for
it, and Tor exits get challenged immediately anyway).

GraphQL query ids rotate when X ships a new web bundle. The current ids are
cached in ``data/x/client.json``; when a call rejects a stale id the scraper
re-extracts them from the live bundle once and retries, so a bundle change
self-heals instead of silently returning nothing.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..models import Item, Target, stable_hash
from .base import Scraper, register
from .fetch import FetchError

log = logging.getLogger(__name__)

_API = "https://x.com/i/api/graphql"
_BUNDLE_RE = re.compile(r"https://abs\.twimg\.com/responsive-web/client-web/main\.[0-9a-f]+\.js")
_QID_RE = "queryId:\"{id}\",operationName:\"{op}\""

# Feature flags the two operations require. X rejects a call that omits one it
# expects ("features cannot be null"); extras are ignored, so this errs long.
_FEATURES_USER: dict[str, bool] = {
    "hidden_profile_subscriptions_enabled": True,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "subscriptions_verification_info_is_identity_verified_enabled": True,
    "subscriptions_verification_info_verified_since_enabled": True,
    "highlights_tweets_tab_ui_enabled": True,
    "responsive_web_twitter_article_notes_tab_enabled": True,
    "subscriptions_feature_can_gift_premium": True,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
}
_FEATURES_TWEETS: dict[str, bool] = {
    "rweb_video_screen_enabled": False,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "premium_content_api_read_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": True,
    "responsive_web_jetfuel_frame": False,
    "responsive_web_grok_share_attachment_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "responsive_web_grok_show_grok_translated_post": False,
    "responsive_web_grok_analysis_button_from_backend": True,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_grok_image_annotation_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}


@register
class TwitterScraper(Scraper):
    """Timelines for ``target.handles`` as Items keyed by tweet id."""

    name = "twitter"

    def scrape(self, target: Target) -> list[Item]:
        s = self.fetcher.settings
        if not (s.x_auth_token and s.x_ct0):
            raise ValueError(
                f"target {target.name!r}: X session not configured "
                "(set ITD_X_AUTH_TOKEN and ITD_X_CT0 in .env)"
            )
        handles = [h.lstrip("@").strip() for h in target.handles if h.strip()]
        if not handles:
            raise ValueError(
                f"target {target.name!r} uses the twitter scraper but lists no handles"
            )

        self._state_dir = s.data_dir / "x"
        self._qids = self._load_qids(s)
        self._users = self._load_users()

        items: list[Item] = []
        for handle in handles:
            try:
                items.extend(self._handle_tweets(target, handle))  # noqa: PERF401
            except FetchError as exc:
                # One bad feed must not sink the rest; the run reports it partial.
                log.warning("x feed @%s failed: %s", handle, exc)
        self._save_users()
        return items

    # -- one feed ------------------------------------------------------------

    def _handle_tweets(self, target: Target, handle: str, _retried: bool = False) -> list[Item]:
        user_id = self._users.get(handle.lower()) or self._resolve(target, handle)
        if not user_id:
            return []
        variables = {
            "userId": user_id, "count": self.fetcher.settings.x_tweet_count,
            "includePromotedContent": True, "withQuickPromoteEligibilityTweetFields": True,
            "withVoice": True, "withV2Timeline": True,
        }
        data = self._graphql(target, self._qids["UserTweets"], "UserTweets",
                             variables, _FEATURES_TWEETS, handle)
        if data is None:
            if not _retried and self._refresh_qids():
                return self._handle_tweets(target, handle, _retried=True)
            return []
        return self._parse_timeline(target.name, data, handle)

    def _resolve(self, target: Target, handle: str) -> str | None:
        data = self._graphql(target, self._qids["UserByScreenName"], "UserByScreenName",
                            {"screen_name": handle}, _FEATURES_USER, handle)
        if data is None and self._refresh_qids():
            data = self._graphql(target, self._qids["UserByScreenName"], "UserByScreenName",
                                {"screen_name": handle}, _FEATURES_USER, handle)
        if not data:
            return None
        rid = (data.get("data", {}).get("user", {}).get("result", {}) or {}).get("rest_id")
        if rid:
            self._users[handle.lower()] = rid
        return rid

    # -- GraphQL -------------------------------------------------------------

    def _graphql(self, target: Target, qid: str, op: str, variables: dict,
                 features: dict, handle: str) -> dict[str, Any] | None:
        s = self.fetcher.settings
        url = (f"{_API}/{qid}/{op}?variables={quote(json.dumps(variables))}"
               f"&features={quote(json.dumps(features))}")
        headers = {
            "authorization": f"Bearer {s.x_bearer}",
            "x-csrf-token": s.x_ct0,
            "x-twitter-active-user": "yes",
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-client-language": "en",
            "content-type": "application/json",
            "referer": f"https://x.com/{handle}",
            "user-agent": s.x_user_agent,
            "cookie": f"auth_token={s.x_auth_token}; ct0={s.x_ct0}",
        }
        try:
            resp = self.fetcher.request("GET", url, headers, network="direct", skip_robots=True)
        except FetchError as exc:
            # A 404 is how a rotated query id presents; signal a refresh.
            if "404" in str(exc):
                return None
            raise
        try:
            payload = resp.json()
        except ValueError:
            return None
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if errors and not payload.get("data"):
            # A stale query id or feature drift: let the caller refresh + retry.
            log.warning("x %s @%s: %s", op, handle, json.dumps(errors)[:200])
            return None
        return payload

    # -- parsing -------------------------------------------------------------

    def _parse_timeline(self, target_name: str, payload: dict, handle: str) -> list[Item]:
        result = payload.get("data", {}).get("user", {}).get("result", {})
        tl = result.get("timeline_v2") or result.get("timeline") or {}
        instructions = tl.get("timeline", {}).get("instructions", [])
        items: list[Item] = []
        seen: set[str] = set()
        for ins in instructions:
            entries = ins.get("entries") or ([ins["entry"]] if ins.get("entry") else [])
            for entry in entries:
                for tweet in self._tweets_in_entry(entry):
                    tid, when, text = tweet
                    if tid in seen:
                        continue
                    seen.add(tid)
                    items.append(self._item(target_name, handle, tid, when, text))
        return items

    @staticmethod
    def _tweets_in_entry(entry: dict) -> list[tuple[str, str, str]]:
        content = entry.get("content", {})
        etype = content.get("entryType")
        if etype == "TimelineTimelineItem":
            cells = [content.get("itemContent", {})]
        elif etype == "TimelineTimelineModule":
            cells = [it.get("item", {}).get("itemContent", {}) for it in content.get("items", [])]
        else:
            return []
        out: list[tuple[str, str, str]] = []
        for cell in cells:
            if cell.get("itemType") != "TimelineTweet":
                continue
            res = cell.get("tweet_results", {}).get("result")
            if not res:
                continue
            res = res.get("tweet", res)
            legacy = res.get("legacy", {})
            tid = res.get("rest_id") or legacy.get("id_str")
            note = (res.get("note_tweet", {}).get("note_tweet_results", {})
                    .get("result", {}).get("text"))
            text = note or legacy.get("full_text") or ""
            if tid and text:
                out.append((tid, legacy.get("created_at", ""), " ".join(text.split())))
        return out

    def _item(self, target_name: str, handle: str, tid: str, when: str, text: str) -> Item:
        # The tweet text is the headline the pipeline classifies and the
        # watchlist matches against; the id is the stable dedupe key.
        return Item(
            key=f"x:{tid}",
            target=target_name,
            title=text,
            url=f"https://x.com/{handle}/status/{tid}",
            text=text,
            fields={
                "source": "x",
                "handle": handle,
                "published": when,          # RFC 2822; dates.py parses it
                "content_hash": stable_hash(text),
            },
        )

    # -- query-id cache ------------------------------------------------------

    def _load_qids(self, settings) -> dict[str, str]:  # noqa: ANN001
        cached = self._read_json(self._state_dir / "client.json")
        return {
            "UserByScreenName": (cached.get("UserByScreenName")
                                 or settings.x_query_user_by_screen_name),
            "UserTweets": cached.get("UserTweets") or settings.x_query_user_tweets,
        }

    def _refresh_qids(self) -> bool:
        """Re-extract query ids from the live web bundle after a stale-id failure."""
        s = self.fetcher.settings
        headers = {
            "user-agent": s.x_user_agent,
            "cookie": f"auth_token={s.x_auth_token}; ct0={s.x_ct0}",
        }
        try:
            home = self.fetcher.request("GET", "https://x.com/home", headers,
                                        network="direct", skip_robots=True)
            m = _BUNDLE_RE.search(home.text)
            if not m:
                return False
            bundle = self.fetcher.request("GET", m.group(0), {"user-agent": s.x_user_agent},
                                          network="direct", skip_robots=True)
        except FetchError as exc:
            log.warning("x query-id refresh failed: %s", exc)
            return False
        found: dict[str, str] = {}
        for op in ("UserByScreenName", "UserTweets"):
            hit = re.search(_QID_RE.format(id="([A-Za-z0-9_-]+)", op=op), bundle.text)
            if hit:
                found[op] = hit.group(1)
        if not found:
            return False
        self._qids.update(found)
        self._state_dir.mkdir(parents=True, exist_ok=True)
        (self._state_dir / "client.json").write_text(json.dumps(self._qids))
        log.info("x query ids refreshed: %s", found)
        return True

    # -- handle->id cache ----------------------------------------------------

    def _load_users(self) -> dict[str, str]:
        return self._read_json(self._state_dir / "users.json")

    def _save_users(self) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        (self._state_dir / "users.json").write_text(json.dumps(self._users))

    @staticmethod
    def _read_json(path: Path) -> dict[str, str]:
        try:
            data = json.loads(path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}


# "x" is the same scraper under the name the user is likely to write.
class XScraper(TwitterScraper):
    name = "x"


register(XScraper)
