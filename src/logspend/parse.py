"""Turn raw log lines into (message, level, timestamp, service) records.

Handles plain text, JSON (structlog/pino/zap/bunyan/logrus style), Docker's json-file
wrapper and the Kubernetes CRI prefix (``<ts> stdout F <line>``).
"""
from __future__ import annotations

import gzip
import io
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Iterator, Optional

MESSAGE_KEYS = ("msg", "message", "@message", "event", "log", "text", "body")
LEVEL_KEYS = ("level", "severity", "lvl", "levelname", "log.level", "severity_text", "loglevel", "@level")
TIME_KEYS = ("time", "timestamp", "ts", "@timestamp", "datetime", "date", "t")
SERVICE_KEYS = ("service.name", "service", "app", "application", "logger", "logger_name", "name", "component")

LEVEL_ALIASES = {
    "trace": "TRACE", "debug": "DEBUG", "dbug": "DEBUG", "verbose": "DEBUG",
    "info": "INFO", "information": "INFO", "notice": "INFO", "informational": "INFO",
    "warn": "WARN", "warning": "WARN",
    "error": "ERROR", "err": "ERROR", "fatal": "FATAL", "critical": "FATAL", "crit": "FATAL",
    "panic": "FATAL", "emerg": "FATAL", "alert": "FATAL",
}
# pino / bunyan numeric levels
NUMERIC_LEVELS = {10: "TRACE", 20: "DEBUG", 30: "INFO", 40: "WARN", 50: "ERROR", 60: "FATAL"}

CRI_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\S+) (?:stdout|stderr) [FP] (.*)$")
ISO_RE = re.compile(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:[.,](\d+))?\s?(Z|[+-]\d{2}:?\d{2})?")
LEVEL_WORD_RE = re.compile(
    r"(?:^|[\s\[|(<])(TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|ERR|FATAL|CRITICAL|PANIC)(?=[\s\]|):>]|$)",
    re.IGNORECASE,
)
KV_LEVEL_RE = re.compile(r"\blevel=\"?(\w+)", re.IGNORECASE)


@dataclass
class Record:
    raw: str          # the line as shipped (without trailing newline)
    size: int         # bytes billed: utf-8 length + 1 for the newline
    message: str      # the human message used for templating
    level: str        # TRACE/DEBUG/INFO/WARN/ERROR/FATAL or "-" if unknown
    ts: Optional[float]
    service: str
    is_json: bool


def norm_level(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "-"
    if isinstance(v, (int, float)):
        return NUMERIC_LEVELS.get(int(v), "-")
    s = str(v).strip().lower()
    if s.isdigit():
        return NUMERIC_LEVELS.get(int(s), "-")
    return LEVEL_ALIASES.get(s, "-")


def parse_time(v) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        x = float(v)
        if x > 1e17:   # ns
            return x / 1e9
        if x > 1e14:   # us
            return x / 1e6
        if x > 1e11:   # ms
            return x / 1e3
        if x > 1e8:
            return x
        return None
    s = str(v)
    m = ISO_RE.search(s)
    if not m:
        try:
            return parse_time(float(s))
        except ValueError:
            return None
    date, clock, frac, tz = m.groups()
    try:
        dt = datetime.strptime(f"{date} {clock}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    off = 0
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        t = tz[1:].replace(":", "")
        off = sign * (int(t[:2]) * 3600 + int(t[2:4]) * 60)
    base = dt.replace(tzinfo=timezone.utc).timestamp() - off
    if frac:
        base += float("0." + frac)
    return base


def _get(d: dict, key: str):
    if key in d:
        return d[key]
    if "." in key:  # nested lookup, e.g. service.name -> {"service": {"name": ...}}
        cur = d
        for part in key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return None
            cur = cur[part]
        return cur
    return None


def _first(d: dict, keys, want_str=False):
    for k in keys:
        v = _get(d, k)
        if v is None:
            continue
        if want_str and not isinstance(v, str):
            continue
        return v
    return None


def parse_line(raw: str, message_key: Optional[str] = None) -> Record:
    line = raw.rstrip("\r\n")
    size = len(line.encode("utf-8", "replace")) + 1
    body = line
    ts = None
    m = CRI_RE.match(line)
    if m:
        ts = parse_time(m.group(1))
        body = m.group(2)
    obj = None
    stripped = body.lstrip()
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
        except ValueError:
            obj = None
    if isinstance(obj, dict):
        # docker json-file wrapper: {"log": "...\n", "stream": "stdout", "time": "..."}
        if set(obj) <= {"log", "stream", "time", "attrs"} and isinstance(obj.get("log"), str):
            inner = parse_line(obj["log"], message_key)
            inner.raw, inner.size = line, size
            if inner.ts is None:
                inner.ts = parse_time(obj.get("time"))
            return inner
        keys = (message_key,) if message_key else MESSAGE_KEYS
        msg = _first(obj, keys, want_str=True)
        if msg is None:
            # no message field: template the key set so identical shapes group together
            msg = "{" + ",".join(sorted(obj)) + "}"
        lvl = norm_level(_first(obj, LEVEL_KEYS))
        if lvl == "-":
            lvl = _level_from_text(msg)
        t = parse_time(_first(obj, TIME_KEYS))
        svc = _first(obj, SERVICE_KEYS, want_str=True)
        return Record(line, size, msg, lvl, t if t is not None else ts, str(svc) if svc is not None else "-", True)
    msg = body
    lvl = "-"
    km = KV_LEVEL_RE.search(body)
    if km:
        lvl = norm_level(km.group(1))
    if lvl == "-":
        lvl = _level_from_text(body)
    if ts is None:
        im = ISO_RE.match(body.lstrip("[ "))
        if im:
            ts = parse_time(im.group(0))
    kvm = re.search(r"\bmsg=\"((?:[^\"\\]|\\.)*)\"", body)
    if kvm:  # logfmt (logrus / go-kit): use msg="..." as the message
        msg = kvm.group(1)
    else:
        msg = _strip_prefix(body)
    return Record(line, size, msg, lvl, ts, "-", False)


def _level_from_text(text: str) -> str:
    m = LEVEL_WORD_RE.search(text[:120])
    return norm_level(m.group(1)) if m else "-"


PREFIX_RE = re.compile(
    r"^\s*\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\s?(?:Z|[+-]\d{2}:?\d{2})?\]?\s*"
)


def _strip_prefix(body: str) -> str:
    """Drop a leading timestamp so it does not dominate the template (it is masked anyway)."""
    return PREFIX_RE.sub("", body, count=1)


def open_any(path: str) -> io.TextIOBase:
    if path == "-":
        return io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")
    if path.endswith(".gz"):
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8", errors="replace")
    return open(path, "r", encoding="utf-8", errors="replace")


def read_records(paths: Iterable[str], message_key: Optional[str] = None) -> Iterator[Record]:
    for p in paths:
        with open_any(p) as fh:
            for raw in fh:
                if not raw.strip():
                    continue
                yield parse_line(raw, message_key)
