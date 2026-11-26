"""On-disk cache for model extractions, keyed by document content, prompt version, and model.

Makes repeated runs free and byte-identical, and lets reviewers reproduce a run
without an API key when the cache directory is shipped with the outputs.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def default_cache_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "data" / "cache" / "extractions"


class ExtractionCache:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    @staticmethod
    def key(text: str, prompt_version: str, model: str) -> str:
        h = hashlib.sha256()
        h.update(model.encode("utf-8") + b"\n" + prompt_version.encode("utf-8") + b"\n" + text.encode("utf-8"))
        return h.hexdigest()

    def _path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def get(self, key: str) -> list[dict] | None:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data.get("spans") if isinstance(data, dict) else None

    def put(self, key: str, spans: list[dict]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._path(key).write_text(json.dumps({"spans": spans}, sort_keys=True, indent=1), encoding="utf-8")
