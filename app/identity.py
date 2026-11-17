"""表计身份：确认归并、待补身份、误并防护。

采集终端升级后，同一块物理表可能从 `007` 写成 `7`。两种处理都不能接受：

1. 直接按字符串字面量看待 → 同一块表被拆成两条序列，月度用量出现缺口；
2. 把所有纯数字表号一律补零 → 本来不同的表计被误并成一块，用量与电费串账。

本模块把「身份」当成一份需要登记依据的数据，而不是字符串启发式：

- 只有随样例命名的登记表 `<样例名>.aliases.csv`（如 `readings_alias.aliases.csv`）
  里登记了 `alias_id → canonical_id` 且带非空 `basis`（登记依据）的别名，才允许归并；
- 如果别名本身已经是契约里的独立物理表，拒绝归并（`E_ALIAS_INVALID`）；
- 没有登记依据、但形如某只契约表号补零写法的读数，标为「待补身份」
  （`Q_IDENTITY_PENDING`），保留原始表号与疑似目标，**不进入**正式用量与费用；
- 无论归并与否，原始表号 `source_meter_id` 与映射依据 `basis` 都随行落库。
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from . import config, errors, reasons

FIELDS = ("alias_id", "canonical_id", "basis", "source", "effective_at")


@dataclass(frozen=True)
class Alias:
    alias_id: str
    canonical_id: str
    basis: str
    source: str
    effective_at: str

    def payload(self) -> dict:
        return {"alias_id": self.alias_id, "canonical_id": self.canonical_id,
                "basis": self.basis, "source": self.source,
                "effective_at": self.effective_at}

    def effective_instant(self):
        """登记依据的生效时点；未填则返回 None（对所有读数生效）。"""
        if not self.effective_at:
            return None
        from .modules.m2_contract import coerce

        try:
            return coerce.parse_instant(self.effective_at)
        except coerce.CoerceError:
            return None


@dataclass
class Registry:
    entries: dict[str, Alias] = field(default_factory=dict)
    digest: str = ""
    filename: str | None = None
    byte_len: int = 0

    def alias_for(self, meter_id: str) -> Alias | None:
        return self.entries.get(meter_id)

    def payload(self) -> list[dict]:
        return [self.entries[key].payload() for key in sorted(self.entries)]


def registry_filename(sample: str) -> str:
    """身份登记文件与样例同名：`readings_alias.csv` → `readings_alias.aliases.csv`。"""
    return f"{Path(sample).stem}.aliases.csv"


def registry_path(settings, sample: str) -> Path:
    return Path(settings.sample_dir) / registry_filename(sample)


def load_registry(settings, sample: str,
                  contracts: dict) -> Registry:
    """读取并校验登记表；没有登记文件时返回空登记（摘要为空字节的 sha256）。"""
    path = registry_path(settings, sample)
    if not path.is_file():
        return Registry(entries={}, digest=hashlib.sha256(b"").hexdigest(),
                        filename=None, byte_len=0)
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    entries: dict[str, Alias] = {}
    for line, row in enumerate(rows, start=2):
        alias_id = (row.get("alias_id") or "").strip()
        canonical_id = (row.get("canonical_id") or "").strip()
        basis = (row.get("basis") or "").strip()
        if not alias_id or not canonical_id:
            _fail(f"第 {line} 行缺少 alias_id 或 canonical_id", alias_id)
        if alias_id == canonical_id:
            _fail(f"第 {line} 行别名与目标相同：{alias_id}", alias_id)
        if not basis:
            _fail(f"第 {line} 行缺少登记依据（basis）：{alias_id}", alias_id)
        if canonical_id not in contracts:
            _fail(f"第 {line} 行归并目标不在契约：{canonical_id}", alias_id)
        if alias_id in contracts:
            _fail(f"第 {line} 行别名 {alias_id} 本身是契约里的独立表计，拒绝误并",
                  alias_id)
        effective_at = (row.get("effective_at") or "").strip()
        if effective_at:
            from .modules.m2_contract import coerce

            try:
                coerce.parse_instant(effective_at)
            except coerce.CoerceError:
                _fail(f"第 {line} 行生效时间无法解析：{effective_at!r}", alias_id)
        existing = entries.get(alias_id)
        if existing is not None and existing.canonical_id != canonical_id:
            _fail(f"第 {line} 行别名 {alias_id} 指向多个目标表计", alias_id)
        entries[alias_id] = Alias(
            alias_id=alias_id, canonical_id=canonical_id, basis=basis,
            source=(row.get("source") or "").strip(),
            effective_at=effective_at)
    # 链式登记（A→B 且 B→C）不猜第二跳，避免一次登记穿透多块表
    for alias in entries.values():
        if alias.canonical_id in entries:
            _fail(f"别名 {alias.alias_id} 的目标 {alias.canonical_id} 本身也是别名，"
                  f"不支持链式归并", alias.alias_id)
    return Registry(entries=entries, digest=hashlib.sha256(raw).hexdigest(),
                    filename=path.name, byte_len=len(raw))


def _fail(message: str, alias_id: str = "") -> None:
    raise errors.ContractInvalid(message, code="E_ALIAS_INVALID",
                                 detail={"alias_id": alias_id})


def zero_pad_candidates(meter_id: str, contracts: dict) -> list[str]:
    """纯数字表号去掉前导零后与哪些契约表号同值——只是「疑似」，不是归并依据。"""
    text = (meter_id or "").strip()
    if not text.isdigit():
        return []
    stripped = text.lstrip("0") or "0"
    out = []
    for candidate in contracts:
        if candidate == text or not candidate.isdigit():
            continue
        if (candidate.lstrip("0") or "0") == stripped:
            out.append(candidate)
    return sorted(out)


def classify(meter_id: str, contracts: dict,
             registry: Registry | None = None,
             read_at=None) -> dict:
    """返回这一行的身份判定：exact / aliased / pending / unknown。

    `read_at`（UTC datetime）落在登记依据的生效时点之前时，归并降级为待补：
    升级前出现过的 `7` 可能是另一块表，不能拿升级后的依据去追认它。
    """
    registry = registry or Registry()
    if meter_id in contracts:
        return {"identity": "exact", "canonical_id": meter_id,
                "source_meter_id": meter_id, "basis": "", "candidates": []}
    alias = registry.alias_for(meter_id)
    if alias is not None:
        effective = alias.effective_instant()
        if effective is not None and read_at is not None and read_at < effective:
            return {"identity": "pending", "canonical_id": None,
                    "source_meter_id": meter_id, "basis": "",
                    "candidates": [alias.canonical_id],
                    "reason": (f"读数早于登记生效时点 {alias.effective_at}，"
                               f"不能按该依据归并")}
        return {"identity": "aliased", "canonical_id": alias.canonical_id,
                "source_meter_id": meter_id, "basis": alias.basis,
                "candidates": [alias.canonical_id], "reason": ""}
    candidates = zero_pad_candidates(meter_id, contracts)
    if candidates:
        return {"identity": "pending", "canonical_id": None,
                "source_meter_id": meter_id, "basis": "",
                "candidates": candidates,
                "reason": "没有身份登记依据"}
    return {"identity": "unknown", "canonical_id": None,
            "source_meter_id": meter_id, "basis": "", "candidates": [],
            "reason": "不在表计契约"}


# ---------- 结果视图：已确认归并 / 待补部分 ----------

def merge_summary(readings: list[dict]) -> list[dict]:
    """本批次实际用到的归并：原始表号 → 规范表号、依据、影响行数与月份。

    月份按展示时区（Asia/Shanghai）归属，与月桶口径一致：UTC 月末的读数
    可能属于本地下一个月，不能按字符串前 7 位切。
    """
    from .modules.m5_months import weights

    buckets: dict[tuple, dict] = {}
    for row in readings:
        if row.get("identity") != "aliased":
            continue
        key = (row.get("source_meter_id") or row.get("raw_meter_id") or row["meter_id"],
               row["meter_id"])
        bucket = buckets.setdefault(key, {
            "source_meter_id": key[0], "meter_id": key[1],
            "basis": row.get("identity_basis") or "",
            "readings": 0, "months": set(), "first_read_at": None,
            "last_read_at": None,
        })
        bucket["readings"] += 1
        stamp = row.get("read_at") or ""
        if stamp:
            bucket["months"].add(weights.month_of(
                dt.datetime.fromisoformat(stamp)))
        if stamp and (bucket["first_read_at"] is None
                      or stamp < bucket["first_read_at"]):
            bucket["first_read_at"] = stamp
        if stamp and (bucket["last_read_at"] is None
                      or stamp > bucket["last_read_at"]):
            bucket["last_read_at"] = stamp
    out = []
    for bucket in buckets.values():
        out.append({**bucket, "months": sorted(bucket["months"]),
                    "month_count": len(bucket["months"])})
    return sorted(out, key=lambda item: (item["meter_id"], item["source_meter_id"]))


def pending_summary(rows: list[dict], contracts: dict) -> list[dict]:
    """待补身份：按原始表号汇总，给出疑似目标与「参考用量（未计入正式结果）」。

    参考用量只在该原始表号自己的序列内用同一套翻转规则推算，再按覆盖天数切到
    自然月，便于月底补登记后核对；它不会进入区间账、月度账与账单。
    """
    from .modules.m4_intervals import rollover
    from .modules.m5_months import weights

    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row.get("source_meter_id") or row["meter_id"],
                           []).append(row)
    out: list[dict] = []
    for raw_id in sorted(grouped):
        items = sorted(grouped[raw_id], key=lambda item: item["read_at"])
        candidate = items[0].get("candidate_id") or ""
        candidates = [item for item in (items[0].get("candidates") or [])
                      if item]
        reference = Decimal("0")
        reference_months: dict[str, Decimal] = {}
        notes: list[str] = []
        contract = contracts.get(candidate) if candidate else None
        if contract is not None and contract.primary_register in {
                item["register"] for item in items}:
            series = sorted((item for item in items
                             if item["register"] == contract.primary_register),
                            key=lambda item: item["read_at"])
            previous = None
            for item in series:
                if previous is not None:
                    verdict = rollover.classify_jump(
                        previous["reading_norm"], item["reading_norm"],
                        contract.capacity)
                    if verdict == "rollover":
                        delta = rollover.rollover_delta(
                            previous["reading_norm"], item["reading_norm"],
                            contract.capacity)
                        notes.append("含满量翻转")
                    elif verdict == "forward":
                        delta = item["reading_norm"] - previous["reading_norm"]
                    else:
                        delta = None
                        notes.append("序列内部倒走，参考用量不可算")
                    if delta is not None:
                        usage = delta * contract.multiplier
                        if contract.unit == "MWh":
                            usage *= 1000
                        reference += usage
                        _add_months(reference_months, usage, weights,
                                    previous["read_at"], item["read_at"])
                previous = item
        out.append({
            "source_meter_id": raw_id,
            "meter_id": raw_id,
            "candidate_id": candidate,
            "candidates": candidates or ([candidate] if candidate else []),
            "basis": "",
            "readings": len(items),
            "months": sorted({
                weights.month_of(dt.datetime.fromisoformat(item["read_at"]))
                for item in items if item.get("read_at")}),
            "first_read_at": items[0]["read_at"],
            "last_read_at": items[-1]["read_at"],
            "reference_usage_kwh": str(reference.quantize(config.USAGE_QUANT)),
            "reference_by_month": {month: str(value.quantize(config.USAGE_QUANT))
                                   for month, value in sorted(reference_months.items())},
            "reference_note": "；".join(dict.fromkeys(notes)),
            "code": "Q_IDENTITY_PENDING",
            "label": reasons.label("Q_IDENTITY_PENDING"),
        })
    return out


def _add_months(bucket: dict, usage, weights, start: str, end: str) -> None:
    """参考用量按天数切月；只作补登记后的核对量，不参与正式账单。"""
    left = dt.datetime.fromisoformat(start)
    right = dt.datetime.fromisoformat(end)
    if right <= left:
        return
    slices = weights.month_slices(left, right)
    if not slices:
        return
    total_weight = sum((weight for _, weight in slices), Decimal("0"))
    for month, weight in slices:
        bucket[month] = bucket.get(month, Decimal("0")) + usage * weight / total_weight


def pending_by_month(pending: list[dict]) -> dict[str, dict]:
    """待补部分按月的汇总（参考量，明确标注未计入正式结果）。"""
    out: dict[str, dict] = {}
    for series in pending:
        for month, value in (series.get("reference_by_month") or {}).items():
            bucket = out.setdefault(month, {
                "month": month, "reference_usage_kwh": Decimal("0"),
                "series": [], "source_meter_ids": [], "included": False,
                "note": "参考用量，未计入正式用量与费用",
            })
            bucket["series"].append(series["source_meter_id"])
            if series["source_meter_id"] not in bucket["source_meter_ids"]:
                bucket["source_meter_ids"].append(series["source_meter_id"])
            bucket["reference_usage_kwh"] += Decimal(value)
    result = {}
    for month, bucket in sorted(out.items()):
        bucket["series"] = sorted(bucket["series"])
        bucket["source_meter_ids"] = sorted(bucket["source_meter_ids"])
        bucket["reference_usage_kwh"] = str(
            bucket["reference_usage_kwh"].quantize(config.USAGE_QUANT))
        result[month] = bucket
    return result


def merged_evidence(intervals: list[dict],
                    merged_summary: list[dict] | None = None) -> dict[str, list[dict]]:
    """按月份列出「本月区间实际用到的归并」（原表号 → 规范表号 + 依据）。

    判据是**区间来源**而非读数所在月：一段 2 月→3 月的区间两端分别由 `007`
    与 `7` 抄得，那么 2 月和 3 月的用量都依赖这次归并，两个月都要能追到依据。
    """
    from .modules.m5_months import weights

    summary = {(item["meter_id"], item["source_meter_id"]): item
               for item in (merged_summary or [])}
    out: dict[str, dict[tuple, dict]] = {}
    for interval in intervals:
        pairs = [(interval["meter_id"], source)
                 for source in (interval.get("source_meter_ids") or [])
                 if source != interval["meter_id"]]
        if not pairs:
            continue
        start = dt.datetime.fromisoformat(interval["start_at"])
        end = dt.datetime.fromisoformat(interval["end_at"])
        for month, weight in weights.month_slices(start, end):
            if weight <= 0:
                continue
            bucket = out.setdefault(month, {})
            for pair in pairs:
                record = summary.get(pair) or {}
                bucket.setdefault(pair, {
                    "source_meter_id": pair[1], "meter_id": pair[0],
                    "basis": record.get("basis", ""),
                    "readings": record.get("readings", 0),
                    "first_read_at": record.get("first_read_at"),
                    "last_read_at": record.get("last_read_at"),
                })
    return {month: [items[key] for key in sorted(items)]
            for month, items in sorted(out.items())}


__all__ = ["Alias", "Registry", "registry_filename", "registry_path",
           "load_registry", "zero_pad_candidates", "classify", "merge_summary",
           "merged_evidence", "pending_summary", "pending_by_month", "FIELDS"]
