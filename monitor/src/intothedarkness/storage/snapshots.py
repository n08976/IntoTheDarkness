"""On-demand content retention.

Default is hash-only: we record a digest and metadata, never the body. Storing
a snapshot is an explicit act — a target set to ``content_mode: store``, or an
``itd snapshot fetch``. Monitoring hidden services can pull in material you do
not want sitting on disk, so keeping bodies is opt-in and easy to purge.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..config import Settings, get_settings
from ..tor import redact

log = logging.getLogger(__name__)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(slots=True)
class Snapshot:
    """A stored body, or the record of one we chose not to store."""

    target: str
    url: str
    sha256: str
    bytes_len: int
    stored: bool
    path: Path | None
    captured_at: datetime
    content_type: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "url": self.url,
            "sha256": self.sha256,
            "bytes": self.bytes_len,
            "stored": self.stored,
            "path": str(self.path) if self.path else None,
            "captured_at": self.captured_at.isoformat(),
            "content_type": self.content_type,
            "note": self.note,
        }


class SnapshotStore:
    """Content-addressed storage under ``data/snapshots/<target>/``.

    Bodies are gzipped and named by digest, so re-capturing unchanged content
    costs nothing and the same body is never stored twice.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def root(self) -> Path:
        path = self.settings.data_dir / "snapshots"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def target_dir(self, target: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in target)[:120]
        path = self.root / (safe or "unnamed")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def capture(
        self,
        target: str,
        url: str,
        content: bytes,
        store: bool,
        content_type: str = "",
        note: str = "",
    ) -> Snapshot:
        """Hash the body always; write it only when ``store`` is true."""
        digest = sha256_bytes(content)
        snapshot = Snapshot(
            target=target,
            url=url,
            sha256=digest,
            bytes_len=len(content),
            stored=False,
            path=None,
            captured_at=datetime.now(UTC),
            content_type=content_type,
            note=note,
        )

        if not store:
            return snapshot

        if len(content) > self.settings.snapshot_max_bytes:
            snapshot.note = (
                f"not stored: {len(content)} bytes exceeds "
                f"snapshot_max_bytes ({self.settings.snapshot_max_bytes})"
            )
            log.warning("snapshot for %s too large to store", target)
            return snapshot

        destination = self.target_dir(target) / f"{digest[:16]}.gz"
        if not destination.exists():
            destination.write_bytes(gzip.compress(content))
        self._write_meta(destination, snapshot, url)

        snapshot.stored = True
        snapshot.path = destination
        return snapshot

    def _write_meta(self, body_path: Path, snapshot: Snapshot, url: str) -> None:
        meta_path = body_path.with_suffix(".json")
        payload = snapshot.to_dict()
        payload["stored"] = True
        payload["path"] = str(body_path)
        # Onion addresses are redacted in the sidecar so an exported metadata
        # dump does not publish the target list.
        payload["url"] = redact(url, self.settings.redact_onion_in_logs)
        payload["url_sha256"] = sha256_bytes(url.encode())
        meta_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def read(self, target: str, digest: str) -> bytes | None:
        """Recover a stored body by digest prefix."""
        directory = self.target_dir(target)
        for candidate in directory.glob(f"{digest[:16]}*.gz"):
            return gzip.decompress(candidate.read_bytes())
        return None

    def list(self, target: str | None = None) -> list[dict]:
        directories = [self.target_dir(target)] if target else sorted(
            p for p in self.root.iterdir() if p.is_dir()
        )
        out: list[dict] = []
        for directory in directories:
            for meta in sorted(directory.glob("*.json")):
                try:
                    out.append(json.loads(meta.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    continue
        return out

    def purge(self, target: str | None = None) -> int:
        """Delete stored bodies. Returns how many files were removed."""
        directories = [self.target_dir(target)] if target else [
            p for p in self.root.iterdir() if p.is_dir()
        ]
        removed = 0
        for directory in directories:
            for path in list(directory.glob("*.gz")) + list(directory.glob("*.json")):
                path.unlink()
                removed += 1
        return removed
