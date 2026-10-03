from __future__ import annotations

from intothedarkness.scrapers.twitter import TwitterScraper


def _tweet_entry(tid, text, when="Fri Oct 03 12:00:00 +0000 2026"):
    return {
        "content": {
            "entryType": "TimelineTimelineItem",
            "itemContent": {
                "itemType": "TimelineTweet",
                "tweet_results": {"result": {
                    "rest_id": tid,
                    "legacy": {"id_str": tid, "full_text": text, "created_at": when},
                }},
            },
        }
    }


def _module_entry(pairs):
    return {
        "content": {
            "entryType": "TimelineTimelineModule",
            "items": [
                {"item": {"itemContent": {
                    "itemType": "TimelineTweet",
                    "tweet_results": {"result": {
                        "rest_id": tid,
                        "legacy": {"id_str": tid, "full_text": text,
                                   "created_at": "Fri Oct 03 11:00:00 +0000 2026"},
                    }},
                }}} for tid, text in pairs
            ],
        }
    }


def _payload(entries):
    return {"data": {"user": {"result": {"timeline": {"timeline": {
        "instructions": [{"type": "TimelineAddEntries", "entries": entries}]
    }}}}}}


def test_parse_timeline_reads_items_and_modules_and_dedupes():
    s = TwitterScraper(fetcher=None)  # parsing never touches the fetcher
    payload = _payload([
        _tweet_entry("111", "hospital breach claimed by qilin"),
        _module_entry([("222", "second tweet"), ("222", "dup id, dropped")]),
        {"content": {"entryType": "TimelineTimelineCursor"}},  # ignored
    ])
    items = s._parse_timeline("x-feeds", payload, "FalconFeedsio")
    ids = sorted(i.key for i in items)
    assert ids == ["x:111", "x:222"]
    one = next(i for i in items if i.key == "x:111")
    assert one.target == "x-feeds"
    assert one.url == "https://x.com/FalconFeedsio/status/111"
    assert one.fields["source"] == "x" and one.fields["handle"] == "FalconFeedsio"
    assert one.title == "hospital breach claimed by qilin"


def test_note_tweet_longform_text_wins_over_truncated_legacy():
    s = TwitterScraper(fetcher=None)
    e = _tweet_entry("333", "truncated…")
    e["content"]["itemContent"]["tweet_results"]["result"]["note_tweet"] = {
        "note_tweet_results": {"result": {"text": "the full long form text"}}
    }
    items = s._parse_timeline("x-feeds", _payload([e]), "vxunderground")
    assert items[0].title == "the full long form text"
