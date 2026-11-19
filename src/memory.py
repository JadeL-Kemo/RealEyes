"""Memory layers for RealEyes.

M0 raw events : ``<root>/events/events.jsonl`` (append-only, written by the service)
M1 index      : ``<root>/state/realeyes.db`` (SQLite, written by the service)
M2 digest     : ``<root>/digests/*.md`` (hourly / daily local-LLM summaries)
M3 recall     : keyword + time search over M0 (and digest text), used by the agent
                and by ``realeyes.py --recall TEXT --hours N``.

The MVP keeps memory deliberately simple and inspectable: every fact is a JSON
event; digests are plain markdown. No embeddings, no hidden store.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path


def _parse_ts(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:  # older rows may be naive; interpret as local time
        dt = dt.astimezone()
    return dt


def _since(hours: float) -> datetime:
    return datetime.now().astimezone() - timedelta(hours=float(hours))


def iter_events(events_jsonl: str | Path):
    path = Path(events_jsonl)
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def recall(events_jsonl: str | Path, query: str, hours: float = 24.0,
           limit: int = 30) -> list[dict]:
    """M3: newest-first events matching ``query`` (case-insensitive substring)."""
    since = _since(hours)
    q = (query or "").strip().lower()
    hits: list[dict] = []
    for ev in iter_events(events_jsonl):
        ts = _parse_ts(ev.get("ts", ""))
        if ts is not None and ts < since:
            continue
        if q and q not in json.dumps(ev, ensure_ascii=False).lower():
            continue
        hits.append(ev)
    hits.sort(key=lambda e: str(e.get("ts", "")), reverse=True)
    return hits[: int(limit)]


def stats(events_jsonl: str | Path, hours: float = 24.0) -> dict:
    since = _since(hours)
    kinds: dict[str, int] = {}
    total = 0
    for ev in iter_events(events_jsonl):
        ts = _parse_ts(ev.get("ts", ""))
        if ts is not None and ts < since:
            continue
        total += 1
        kind = str(ev.get("kind", "?"))
        kinds[kind] = kinds.get(kind, 0) + 1
    return {"events": total, "kinds": kinds}


def context_lines(events_jsonl: str | Path, hours: float = 6.0,
                  max_events: int = 12) -> list[str]:
    """Compact M0 context lines for the local digest prompt (M2 input)."""
    since = _since(hours)
    rows: list[str] = []
    for ev in iter_events(events_jsonl):
        ts = _parse_ts(ev.get("ts", ""))
        if ts is not None and ts < since:
            continue
        rows.append("{} [{}] {}: {}".format(
            ev.get("ts", ""), ev.get("severity", "?"), ev.get("kind", "?"),
            str(ev.get("summary", ""))[:220]))
    return rows[-int(max_events):]
