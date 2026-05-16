from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any


class TradeLogger:
    """JSONL logger with in-process rotation."""

    def __init__(self, path: Path, max_bytes: int = 50 * 1024 * 1024) -> None:
        self.path = path
        self.max_bytes = max_bytes

    def write(self, event: str, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Rotate before write if current file exceeds max_bytes.
        # Safe: we open/append/close each line, so there is no persistent file handle.
        if self.path.exists() and self.path.stat().st_size > self.max_bytes:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            rotated = self.path.with_name(f"{self.path.stem}.{ts}.jsonl")
            os.replace(self.path, rotated)
            self._cleanup_old_rotations()
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **payload,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    def _cleanup_old_rotations(self, keep: int = 7) -> None:
        """Delete oldest rotated logs beyond `keep` most recent files."""
        stem = self.path.stem
        suffix = ".jsonl"
        pattern = f"{stem}.*{suffix}"
        rotated = sorted(
            [p for p in self.path.parent.glob(pattern) if p != self.path],
            key=lambda p: p.stat().st_mtime,
        )
        for old in rotated[:-keep]:
            try:
                old.unlink()
            except OSError:
                pass
