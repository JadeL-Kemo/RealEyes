"""File-change watcher for RealEyes (MVP: new / changed files in selected folders).

Part of the RealEyes project. Kept dependency-free (stdlib only) so it can run
inside the single-file service process on Windows / low-end machines.
"""
from __future__ import annotations

import fnmatch
import os
import time
from pathlib import Path

SKIP_DIRS = {"node_modules", ".git", ".cache", "temp", "tmp", "__pycache__",
             "System Volume Information", "$RECYCLE.BIN"}


class FileWatch:
    """Snapshot/diff watcher over a small set of folders.

    The first scan only primes the snapshot (no "new" burst at startup).
    Call ``scan()`` periodically; it returns new and changed paths since the
    previous call.
    """

    def __init__(self, dirs: list[str], patterns: list[str] | None = None,
                 max_files: int = 5000) -> None:
        self.dirs = [Path(d) for d in (dirs or []) if str(d).strip()]
        self.patterns = [p for p in (patterns or ["*"]) if str(p).strip()] or ["*"]
        self.max_files = int(max_files)
        self._snap: dict[str, tuple[float, int]] = {}
        self._primed = False
        self.last_scan = 0.0

    def _match(self, name: str) -> bool:
        low = name.lower()
        return any(fnmatch.fnmatch(low, p.lower()) for p in self.patterns)

    def _walk(self) -> dict[str, tuple[float, int]]:
        out: dict[str, tuple[float, int]] = {}
        for d in self.dirs:
            if not d.is_dir():
                continue
            count = 0
            for root, dirs, files in os.walk(d):
                dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not x.startswith(".")]
                for f in files:
                    if not self._match(f):
                        continue
                    p = Path(root) / f
                    try:
                        st = p.stat()
                    except OSError:
                        continue
                    out[str(p)] = (st.st_mtime, st.st_size)
                    count += 1
                    if count >= self.max_files:
                        break
                if count >= self.max_files:
                    break
        return out

    def scan(self) -> dict:
        cur = self._walk()
        if not self._primed:
            self._snap = cur
            self._primed = True
            self.last_scan = time.time()
            return {"new": [], "changed": [], "total": len(cur)}
        new = sorted(p for p in cur if p not in self._snap)
        changed = sorted(p for p in cur
                         if p in self._snap and cur[p][0] > self._snap[p][0] + 1e-6)
        self._snap = cur
        self.last_scan = time.time()
        return {"new": new, "changed": changed, "total": len(cur)}
