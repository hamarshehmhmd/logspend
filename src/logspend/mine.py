"""Group log records into templates, cost them, and suggest drop/sample/keep actions."""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from .parse import Record

# Order matters: the most specific masks first.
MASKS: List[Tuple[str, "re.Pattern[str]"]] = [
    ("<TS>", re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?")),
    ("<UUID>", re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")),
    ("<EMAIL>", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    ("<IP>", re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?(?![\d.])")),
    ("<HEX>", re.compile(r"\b(?:0x)?(?=[0-9a-fA-F]*\d)(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{8,}\b")),
    ("<NUM>", re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?:[a-zA-Z%]{1,3})?(?![\w.])")),
]
# Exported regex fragments. Kept to the common subset of RE2 (OTel/Go), Rust regex (Vector),
# Onigmo (Fluent Bit) and Python re: no lookarounds, no backreferences, no possessives.
PLACEHOLDER_RE = {
    "<TS>": r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?",
    "<UUID>": r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
    "<EMAIL>": r"[A-Za-z0-9_.+-]+@[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+",
    "<IP>": r"\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?",
    "<HEX>": r"(?:0x)?[0-9a-fA-F]{8,}",
    "<NUM>": r"-?\d+(?:\.\d+)?[a-zA-Z%]*",
    "<*>": r"\S+",
}
PH_SPLIT = re.compile(r"(<TS>|<UUID>|<EMAIL>|<IP>|<HEX>|<NUM>|<\*>)")

LEVEL_RANK = {"TRACE": 0, "DEBUG": 1, "-": 2, "INFO": 2, "WARN": 3, "ERROR": 4, "FATAL": 5}
ERROR_WORDS = re.compile(
    r"(?i)\b(error|errors|exception|traceback|fail|failed|failure|fatal|panic|timeout|timed out|"
    r"denied|refused|unauthori[sz]ed|forbidden|crash|abort|deadlock|oom|killed|invalid|unavailable)\b"
)
HEALTH_WORDS = re.compile(
    r"(?i)(/healthz?\b|/health/|/readyz?\b|/livez?\b|/ready\b|/live\b|/ping\b|/status\b|/metrics\b|"
    r"\bhealth ?check|\breadiness\b|\bliveness\b|kube-probe|ELB-HealthChecker|GoogleHC|\bheartbeat\b)"
)

PRICING = {
    # Approximate public list prices (USD). Override with --price-gb / --price-per-million.
    "datadog": {"per_gb": 0.10, "per_million": 1.70, "note": "Datadog: $0.10/GB ingest + $1.70/M indexed events (15-day, annual)"},
    "cloudwatch": {"per_gb": 0.50, "per_million": 0.0, "note": "CloudWatch Logs standard ingestion $0.50/GB (us-east-1)"},
    "newrelic": {"per_gb": 0.40, "per_million": 0.0, "note": "New Relic data ingest ~$0.40/GB beyond the free tier"},
    "grafana": {"per_gb": 0.50, "per_million": 0.0, "note": "Grafana Cloud logs ~$0.50/GB"},
    "generic": {"per_gb": 0.50, "per_million": 0.0, "note": "generic $0.50/GB"},
}
MONTH = 30 * 86400


def mask(message: str) -> str:
    s = message
    for ph, rx in MASKS:
        s = rx.sub(ph, s)
    return " ".join(s.split())


@dataclass
class Pattern:
    template: str
    count: int = 0
    bytes: int = 0
    levels: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    services: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    json_count: int = 0
    errorish: bool = False
    example: str = ""
    id: str = ""
    action: str = "keep"
    sample_keep: int = 0
    reason: str = ""
    regex: str = ""

    @property
    def level(self) -> str:
        if not self.levels:
            return "-"
        return max(self.levels.items(), key=lambda kv: (kv[1], LEVEL_RANK.get(kv[0], 2)))[0]

    @property
    def max_level(self) -> str:
        return max(self.levels, key=lambda k: LEVEL_RANK.get(k, 2)) if self.levels else "-"

    def add(self, r: Record) -> None:
        self.count += 1
        self.bytes += r.size
        self.levels[r.level] += 1
        self.services[r.service] += 1
        if r.is_json:
            self.json_count += 1
        if not self.errorish and (LEVEL_RANK.get(r.level, 2) >= 3 or ERROR_WORDS.search(r.message)):
            self.errorish = True
        if not self.example:
            self.example = r.message[:300]

    def absorb(self, other: "Pattern") -> None:
        self.count += other.count
        self.bytes += other.bytes
        for k, v in other.levels.items():
            self.levels[k] += v
        for k, v in other.services.items():
            self.services[k] += v
        self.json_count += other.json_count
        self.errorish = self.errorish or other.errorish
        self.example = self.example or other.example


def _mergeable(tok: str) -> bool:
    if tok.startswith("/") or tok.startswith("<"):
        return False
    if ERROR_WORDS.search(tok) or HEALTH_WORDS.search(tok):
        return False
    return tok.upper() not in LEVEL_RANK


def merge_high_cardinality(patterns: Dict[str, Pattern], threshold: int = 5) -> Dict[str, Pattern]:
    """Wildcard token positions that take >= threshold distinct values among otherwise-identical templates."""
    if threshold <= 0:
        return patterns
    changed = True
    while changed:
        changed = False
        by_len: Dict[int, List[str]] = defaultdict(list)
        for t in patterns:
            by_len[len(t.split(" "))].append(t)
        for n, temps in by_len.items():
            if len(temps) < threshold:
                continue
            for i in range(n):
                groups: Dict[tuple, List[str]] = defaultdict(list)
                for t in temps:
                    toks = t.split(" ")
                    if not _mergeable(toks[i]):
                        continue
                    groups[tuple(toks[:i] + toks[i + 1:])].append(t)
                for key, members in groups.items():
                    if len(members) < threshold:
                        continue
                    toks = list(key)
                    toks.insert(i, "<*>")
                    new_t = " ".join(toks)
                    target = patterns.get(new_t) or Pattern(new_t)
                    for m in members:
                        if m in patterns and m != new_t:
                            target.absorb(patterns.pop(m))
                    patterns[new_t] = target
                    changed = True
                if changed:
                    break
            if changed:
                break
    return patterns


def _esc(s: str, json_tolerant: bool) -> str:
    out = []
    for ch in s:
        if ch in ".^$*+?()[]{}|":
            out.append("\\" + ch)
        elif ch == "\\":
            out.append(r"\\{1,2}" if json_tolerant else r"\\")
        elif ch == '"':
            out.append(r"\\?\x22" if json_tolerant else r"\x22")
        elif ch == "'":
            out.append(r"\x27")
        elif ch == "/" and json_tolerant:
            out.append(r"\\?/")
        elif ch == "#":
            out.append(r"\x23")
        elif ord(ch) < 32 or ord(ch) == 127:
            out.append("\\x%02x" % ord(ch))
        else:
            out.append(ch)
    return "".join(out)


def template_regex(template: str, json_tolerant: bool = False) -> str:
    """Unanchored regex that matches the raw line (plain or JSON) carrying this template."""
    sep = r"\s+"
    parts = []
    for tok in template.split(" "):
        frag = []
        for piece in PH_SPLIT.split(tok):
            if not piece:
                continue
            frag.append(PLACEHOLDER_RE[piece] if piece in PLACEHOLDER_RE else _esc(piece, json_tolerant))
        parts.append("".join(frag))
    return sep.join(parts)


def mine(records: Iterable[Record], merge_threshold: int = 5):
    patterns: Dict[str, Pattern] = {}
    tmin: Optional[float] = None
    tmax: Optional[float] = None
    total = 0
    for r in records:
        total += 1
        t = mask(r.message) or "<empty>"
        p = patterns.get(t)
        if p is None:
            p = patterns[t] = Pattern(t)
        p.add(r)
        if r.ts is not None:
            tmin = r.ts if tmin is None or r.ts < tmin else tmin
            tmax = r.ts if tmax is None or r.ts > tmax else tmax
    patterns = merge_high_cardinality(patterns, merge_threshold)
    out = sorted(patterns.values(), key=lambda p: (-p.bytes, -p.count, p.template))
    for i, p in enumerate(out, 1):
        p.id = f"p{i}"
        p.regex = template_regex(p.template, json_tolerant=p.json_count > 0)
    window = (tmax - tmin) if (tmin is not None and tmax is not None and tmax > tmin) else None
    return out, window, total


def monthly_cost(bytes_: float, count: float, window_s: float, per_gb: float, per_million: float) -> float:
    scale = MONTH / window_s
    return bytes_ * scale / 1e9 * per_gb + count * scale / 1e6 * per_million


def suggest(patterns: List[Pattern], min_share: float = 0.05, min_count: int = 100, sample_keep: int = 10) -> None:
    total_bytes = sum(p.bytes for p in patterns) or 1
    for p in patterns:
        lvl = p.max_level
        if p.errorish or LEVEL_RANK.get(lvl, 2) >= 3:
            p.action, p.reason = "keep", "warn/error or failure wording: never touched"
            continue
        if p.level in ("TRACE", "DEBUG") and lvl in ("TRACE", "DEBUG"):
            p.action, p.reason = "drop", f"{p.level.lower()}-level logs shipped to the vendor"
            continue
        if HEALTH_WORDS.search(p.template):
            p.action, p.reason = "drop", "health check / probe / heartbeat noise"
            continue
        if p.bytes / total_bytes >= min_share and p.count >= min_count:
            p.action, p.sample_keep = "sample", sample_keep
            p.reason = f"high-volume info pattern ({p.bytes / total_bytes:.0%} of bytes): keep 1 in {sample_keep}"
            continue
        p.action, p.reason = "keep", ""
