"""serve/harness/spill.py — oversized tool output to disk + locator (ADR-0004).

saveText writes the full output verbatim into the session's spill directory
and returns an opaque locator; the inline result keeps head/tail. Storage
failure degrades to keeping the inline result — spill never fails the call.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

SPILL_MIN_CHARS = 20_000
HEAD = 4000
TAIL = 4000


class SpillStore:
    def __init__(self, spill_dir: Path) -> None:
        self.dir = spill_dir

    def save_text(self, text: str) -> dict:
        """-> {"locator": str, "bytes": int}. Raises OSError on storage failure."""
        self.dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
        path = self.dir / f"{digest}.txt"
        if not path.exists():
            tmp = path.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
        meta = {"locator": f"spill://{digest}", "bytes": len(text.encode('utf-8', errors='replace'))}
        (self.dir / f"{digest}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
        return meta

    def read(self, locator: str) -> str | None:
        if not locator.startswith("spill://"):
            return None
        path = self.dir / f"{locator[8:]}.txt"
        return path.read_text(encoding="utf-8") if path.exists() else None


def spill_policy(text: str, store: SpillStore) -> dict:
    """Apply the spill seam to one tool result. Returns the (possibly replaced) result payload."""
    if len(text) <= SPILL_MIN_CHARS:
        return {"text": text}
    try:
        meta = store.save_text(text)
    except OSError:
        return {"text": text}          # degrade: keep inline, never fail the call
    return {
        "text": text[:HEAD] + f"\n[... {len(text) - HEAD - TAIL} chars spilled to {meta['locator']} ...]\n"
              + text[-TAIL:],
        "spill": meta,
        "retrievalHint": "read the full text with the spill locator (session-scoped file)",
    }
