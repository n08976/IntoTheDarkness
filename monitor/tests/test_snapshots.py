"""Content retention: hash by default, store on demand."""

from __future__ import annotations

import gzip
import json

from intothedarkness.storage import SnapshotStore, sha256_bytes

BODY = b"<html><body>victim listing</body></html>"
ONION = "duckduckgogg42xjoc72x3sjasowoarfbgcmvfimaftt6twagswzczad.onion"


def store(settings) -> SnapshotStore:
    settings.ensure_dirs()
    return SnapshotStore(settings)


def test_hash_only_is_the_default_and_writes_nothing(settings):
    s = store(settings)
    snap = s.capture("t", "http://x.onion/", BODY, store=False)

    assert snap.stored is False and snap.path is None
    assert snap.sha256 == sha256_bytes(BODY)
    assert snap.bytes_len == len(BODY)
    assert s.list() == []


def test_storing_writes_a_gzipped_body_readable_again(settings):
    s = store(settings)
    snap = s.capture("t", "http://x.onion/", BODY, store=True)

    assert snap.stored and snap.path.exists()
    assert gzip.decompress(snap.path.read_bytes()) == BODY
    assert s.read("t", snap.sha256) == BODY


def test_identical_bodies_are_stored_once(settings):
    s = store(settings)
    first = s.capture("t", "http://x.onion/", BODY, store=True)
    second = s.capture("t", "http://x.onion/", BODY, store=True)
    assert first.path == second.path
    assert len(list(first.path.parent.glob("*.gz"))) == 1


def test_metadata_redacts_the_onion_address(settings):
    s = store(settings)
    snap = s.capture("t", f"http://{ONION}/list", BODY, store=True)
    meta = json.loads(snap.path.with_suffix(".json").read_text())

    assert ONION not in meta["url"]
    assert ".onion" in meta["url"]
    # The full URL is still identifiable by digest without being published.
    assert meta["url_sha256"] == sha256_bytes(f"http://{ONION}/list".encode())


def test_oversized_bodies_are_hashed_but_not_stored(settings):
    settings.snapshot_max_bytes = 10
    s = store(settings)
    snap = s.capture("t", "http://x.onion/", b"x" * 100, store=True)

    assert snap.stored is False
    assert "exceeds" in snap.note
    assert snap.sha256  # still recorded


def test_listing_and_purging(settings):
    s = store(settings)
    s.capture("alpha", "http://a.onion/", b"one", store=True)
    s.capture("beta", "http://b.onion/", b"two", store=True)

    assert len(s.list()) == 2
    assert len(s.list("alpha")) == 1

    assert s.purge("alpha") == 2          # body plus sidecar
    assert len(s.list()) == 1
    s.purge()
    assert s.list() == []


def test_target_names_are_made_filesystem_safe(settings):
    s = store(settings)
    snap = s.capture("../../etc/passwd", "http://x.onion/", BODY, store=True)
    assert snap.stored
    # The traversal is neutralised rather than escaping the snapshot root.
    assert s.root in snap.path.parents
    assert ".." not in snap.path.parts


def test_unknown_digest_reads_as_none(settings):
    assert store(settings).read("t", "deadbeef" * 4) is None
