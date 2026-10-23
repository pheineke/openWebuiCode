"""Usage ledger.

Every chat completion that passes through the proxy leaves one line (tokens, cost). The upstreams
have no quota API for users, so this is the measurable consumption; it also covers opencode.
"""
import datetime
import json
import time

from . import auth

LEDGER = auth.STATE_DIR / "usage.jsonl"
META = auth.STATE_DIR / "usage-meta.json"


def meta() -> dict:
    try:
        return json.loads(META.read_text())
    except (OSError, ValueError):
        m = {"since": int(time.time()), "backfilled": False}   # live capture starts now
        save_meta(m)
        return m


def save_meta(m: dict) -> None:
    auth.STATE_DIR.mkdir(parents=True, exist_ok=True)
    META.write_text(json.dumps(m))


def record(provider: str | None, model: str | None, usage: dict, src: str = "proxy", ts: int | None = None) -> None:
    try:
        row = {"ts": int(ts or time.time()), "provider": provider, "model": model or "?", "src": src,
               "in": int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0),
               "out": int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0),
               "cost": float(usage.get("cost") or 0)}
        auth.STATE_DIR.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a") as f:
            f.write(json.dumps(row) + "\n")
    except (OSError, ValueError, TypeError):
        pass   # accounting must never break a chat


class Tap:
    """Watches a response pass by and records the final `usage` block (SSE chunk or JSON body)."""

    def __init__(self, provider: str | None, model: str | None, streaming: bool):
        self.provider, self.model, self.streaming, self.buf, self.usage = provider, model, streaming, b"", None

    def feed(self, chunk: bytes) -> None:
        if self.streaming:
            self.buf += chunk
            *lines, self.buf = self.buf.split(b"\n")
            for line in lines:
                if line.startswith(b"data:") and b'"usage"' in line:
                    try:
                        u = json.loads(line[5:]).get("usage")
                    except ValueError:
                        continue
                    if u:
                        self.usage = u
        elif len(self.buf) < 5_000_000:
            self.buf += chunk

    def finish(self) -> None:
        if not self.streaming:
            try:
                self.usage = json.loads(self.buf).get("usage")
            except (ValueError, AttributeError):
                pass
        if self.usage:
            record(self.provider, self.model, self.usage)


async def tapped(chunks, tap: Tap):
    try:
        async for chunk in chunks:
            tap.feed(chunk)
            yield chunk
    finally:
        tap.finish()


def _agg(rows: list[dict], names: dict) -> dict:
    models: dict[tuple, dict] = {}
    tot = {"requests": 0, "in": 0, "out": 0, "cost": 0.0}
    for r in rows:
        pid = r.get("provider")
        key = (pid, r["model"])
        m = models.setdefault(key, {"model": r["model"], "provider": pid, "provider_name": names.get(pid),
                                    "requests": 0, "in": 0, "out": 0, "cost": 0.0})
        for d in (m, tot):
            d["requests"] += 1; d["in"] += r["in"]; d["out"] += r["out"]; d["cost"] += r["cost"]
    return {**tot, "models": sorted(models.values(), key=lambda m: -(m["in"] + m["out"]))}


def report(names: dict) -> dict:
    """Totals for this month, all time and the last 30 days.  `names`: provider id -> display name."""
    rows = []
    try:
        rows = [json.loads(l) for l in LEDGER.read_text().splitlines() if l.strip()]
    except (OSError, ValueError):
        pass
    m = meta()
    now = datetime.datetime.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
    days: dict[str, int] = {}
    for r in rows:
        d = datetime.date.fromtimestamp(r["ts"]).isoformat()
        days[d] = days.get(d, 0) + r["in"] + r["out"]
    last30 = [(datetime.date.today() - datetime.timedelta(days=i)).isoformat() for i in range(29, -1, -1)]
    return {"since": m["since"], "backfilled": m["backfilled"],
            "month": {**_agg([r for r in rows if r["ts"] >= month_start], names), "label": now.strftime("%Y-%m")},
            "total": _agg(rows, names), "days": [{"date": d, "tokens": days.get(d, 0)} for d in last30]}
