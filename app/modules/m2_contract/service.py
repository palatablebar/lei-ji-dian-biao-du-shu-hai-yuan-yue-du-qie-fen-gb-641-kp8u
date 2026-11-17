"""契约解释：单元格文本 → 带单位的读数记录，逐行给出原因码。

读数一律折成契约单位后再交给下游，换算系数只在 units.py 里定义一次。

表号身份分四种（见 `app/identity.py`）：

- `exact`：契约里就是这只表；
- `aliased`：登记表里有依据的别名，归并到规范表号，原表号与依据随行保留；
- `pending`：形如某只表的补零写法但**没有登记依据**，单列为待补，不进正式结果；
- `unknown`：不在契约、也不是任何表的补零写法，按未知表计隔离。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from ... import config, identity, reasons
from . import coerce, contract, units


@dataclass
class Interpreted:
    """三个历史返回值 + 待补身份行；仍可按 `records, quarantine, diagnostics` 解包。"""
    records: list[dict] = field(default_factory=list)
    quarantine: list[dict] = field(default_factory=list)
    diagnostics: list[dict] = field(default_factory=list)
    pending: list[dict] = field(default_factory=list)

    def __iter__(self):
        return iter((self.records, self.quarantine, self.diagnostics))


def interpret_rows(rows: list[dict], contracts: dict[str, contract.MeterContract],
                   registry: identity.Registry | None = None) -> Interpreted:
    registry = registry or identity.Registry()
    out = Interpreted()
    for row in rows:
        cells = row["cells"]
        source_id = coerce.normalize_text(cells.get("meter_id"))
        # 先软解析时间：登记依据有生效时点时，早于生效的读数不能按该依据归并。
        # 解析失败不在这里报错，仍按原顺序交给 Q_BAD_TIME 隔离。
        read_instant = None
        try:
            read_instant = coerce.parse_instant(cells.get("read_at"))
        except coerce.CoerceError:
            pass
        verdict = identity.classify(source_id, contracts, registry, read_instant)
        if verdict["identity"] == "unknown":
            out.quarantine.append(_q(
                row, "Q_UNKNOWN_METER",
                f"表号 {source_id!r} 不在表计契约", source_id, "meter_id"))
            continue
        if verdict["identity"] == "pending":
            pending = _parse_pending(row, source_id, verdict, contracts)
            if isinstance(pending, tuple):
                # 表号可疑之外还有别的硬错误：按那个原因码隔离，不混进「待补」
                out.quarantine.append(pending[0])
            else:
                out.pending.append(pending)
            continue
        canonical_id = verdict["canonical_id"]
        found = contracts[canonical_id]
        try:
            register = coerce.parse_register(cells.get("register"))
        except coerce.CoerceError as exc:
            out.quarantine.append(_q(row, exc.code, exc.detail, source_id, "register"))
            continue
        if register not in found.registers:
            out.quarantine.append(_q(row, "Q_BAD_REGISTER",
                                     f"表 {canonical_id} 契约里没有 {register}",
                                     source_id, "register"))
            continue
        try:
            read_at = coerce.parse_instant(cells.get("read_at"))
        except coerce.CoerceError as exc:
            out.quarantine.append(_q(row, exc.code, exc.detail, source_id, "read_at"))
            continue
        raw_reading = cells.get("reading", "")
        try:
            reading = coerce.parse_decimal(raw_reading)
        except coerce.CoerceError as exc:
            out.quarantine.append(_q(row, exc.code, exc.detail, source_id, "reading"))
            continue
        if reading is None:
            out.quarantine.append(_q(row, "Q_MISSING_READING",
                                     f"读数是空值哨兵 {raw_reading!r}", source_id,
                                     "reading"))
            continue
        unit_raw = coerce.normalize_text(cells.get("unit"))
        flags: list[str] = []
        if verdict["identity"] == "aliased":
            flags.append("K_METER_ID_ALIASED")
        if unit_raw:
            canonical = units.canonical_unit(unit_raw)
            if canonical is None:
                out.quarantine.append(_q(row, "Q_BAD_UNIT",
                                         f"单位无法识别 {unit_raw!r}", source_id,
                                         "unit"))
                continue
            try:
                reading = units.convert(reading, canonical, found.unit)
            except units.UnknownUnit:
                out.quarantine.append(_q(row, "Q_BAD_UNIT",
                                         f"单位无法识别 {unit_raw!r}", source_id,
                                         "unit"))
                continue
            if canonical != found.unit:
                flags.append("K_UNIT_CONVERTED")
        else:
            flags.append("K_UNIT_ASSUMED")
        try:
            read_type = coerce.parse_read_type(cells.get("read_type"))
        except coerce.CoerceError as exc:
            out.quarantine.append(_q(row, exc.code, exc.detail, source_id, "read_type"))
            continue
        out.records.append({
            "lineno": row["lineno"],
            "segment": row["segment"],
            "meter_id": canonical_id,
            "source_meter_id": source_id,
            "identity": verdict["identity"],
            "identity_basis": verdict["basis"],
            "register": register,
            "read_at": coerce.iso_utc(read_at),
            "reading": reading,
            "reading_text": (raw_reading or "").strip(),
            "unit_raw": unit_raw,
            "unit_norm": found.unit,
            "read_type": read_type,
            "raw": row.get("raw", ""),
            "note": coerce.normalize_text(cells.get("note")),
            "flags": flags,
        })
    return out


def _parse_pending(row: dict, source_id: str, verdict: dict,
                   contracts: dict[str, contract.MeterContract],
                   ) -> dict | tuple:
    """待补行也要把其余字段解析干净，才能给出「参考用量」并保留证据。"""
    cells = row["cells"]
    parse_steps = (
        ("register", coerce.parse_register),
        ("read_at", coerce.parse_instant),
        ("reading", coerce.parse_decimal),
        ("read_type", coerce.parse_read_type),
    )
    parsed: dict = {}
    for field_name, parser in parse_steps:
        try:
            parsed[field_name] = parser(cells.get(field_name))
        except coerce.CoerceError as exc:
            return (_q(row, exc.code, exc.detail, source_id, field_name),)
    reading = parsed["reading"]
    if reading is None:
        return (_q(row, "Q_MISSING_READING",
                   f"读数是空值哨兵 {cells.get('reading')!r}", source_id, "reading"),)
    register = parsed["register"]
    read_at = parsed["read_at"]
    read_type = parsed["read_type"]
    raw_reading = cells.get("reading", "")
    unit_raw = coerce.normalize_text(cells.get("unit"))
    # 疑似目标只用于「参考用量」，但仍按目标的契约校验寄存器与单位，
    # 否则补登记后会发现这一行本来也算不出来。
    candidate_id = verdict["candidates"][0]
    candidate = contracts[candidate_id]
    if register not in candidate.registers:
        return (_q(row, "Q_BAD_REGISTER",
                   f"疑似目标 {candidate_id} 契约里没有 {register}", source_id,
                   "register"),)
    if unit_raw:
        canonical = units.canonical_unit(unit_raw)
        if canonical is None:
            return (_q(row, "Q_BAD_UNIT", f"单位无法识别 {unit_raw!r}", source_id,
                       "unit"),)
        reading = units.convert(reading, canonical, candidate.unit)
    return {
        "lineno": row["lineno"], "segment": row["segment"],
        "source_meter_id": source_id, "meter_id": source_id,
        "candidate_id": candidate_id, "candidates": list(verdict["candidates"]),
        "register": register, "read_at": coerce.iso_utc(read_at),
        "reading_norm": reading, "reading_text": (raw_reading or "").strip(),
        "unit_raw": unit_raw, "unit_norm": candidate.unit,
        "read_type": read_type, "identity_basis": "",
        "note": coerce.normalize_text(cells.get("note")),
        "raw": row.get("raw", ""),
        "pending_reason": verdict.get("reason") or "没有身份登记依据",
    }


def pending_payload(rows: list[dict]) -> list[dict]:
    """待补身份的统一视图：页面与下载都从这里取，保证同源。"""
    out = []
    for row in rows:
        reason = row.get("pending_reason") or "没有身份登记依据"
        out.append({
            "lineno": row["lineno"], "segment": row.get("segment", 1),
            "source_meter_id": row["source_meter_id"],
            "candidate_id": row["candidate_id"],
            "candidates": list(row.get("candidates") or []),
            "register": row["register"], "read_at": row["read_at"],
            "reading_text": row["reading_text"],
            "reading_norm": row.get("reading_norm"),
            "unit_raw": row.get("unit_raw") or "",
            "unit_norm": row.get("unit_norm"),
            "read_type": row["read_type"], "note": row.get("note", ""),
            "code": "Q_IDENTITY_PENDING", "level": "warn",
            "label": reasons.label("Q_IDENTITY_PENDING"),
            "detail": row.get("detail") or (
                f"疑似 {row['candidate_id']} 的补零写法，{reason}；"
                f"不计入正式用量与费用"),
        })
    return out


def _q(row: dict, code: str, detail: str, meter_id: str, field: str) -> dict:
    return {"lineno": row["lineno"], "segment": row["segment"], "code": code,
            "stage": "contract", "level": reasons.level(code), "field": field,
            "raw_value": row["cells"].get(field, ""), "meter_id": meter_id,
            "detail": detail}


def preview(cells: dict[str, str], contracts: dict[str, contract.MeterContract]) -> dict:
    """单行试解析：接口用来给使用者看「这行会被读成什么」。"""
    out: dict = {"input": dict(cells), "ok": True, "code": None, "detail": ""}
    meter_id = coerce.normalize_text(cells.get("meter_id"))
    found = contracts.get(meter_id)
    if found is None:
        out.update(ok=False, code="Q_UNKNOWN_METER", detail=f"表号 {meter_id!r} 不在契约")
        return out
    try:
        register = coerce.parse_register(cells.get("register"))
        read_at = coerce.parse_instant(cells.get("read_at"))
        reading = coerce.parse_decimal(cells.get("reading"))
        read_type = coerce.parse_read_type(cells.get("read_type"))
        unit_raw = coerce.normalize_text(cells.get("unit"))
        canonical = units.canonical_unit(unit_raw) if unit_raw else found.unit
        if canonical is None:
            raise coerce.CoerceError("Q_BAD_UNIT", f"单位无法识别 {unit_raw!r}")
        value = None if reading is None else units.convert(reading, canonical, found.unit)
    except coerce.CoerceError as exc:
        out.update(ok=False, code=exc.code, detail=exc.detail)
        return out
    out.update({
        "meter_id": meter_id,
        "register": register,
        "read_at": read_at.isoformat(),
        "reading_text": coerce.normalize_text(cells.get("reading")),
        "reading": None if value is None else str(value),
        "unit": found.unit,
        "read_type": read_type,
        "is_estimate": read_type == "E",
        "capacity": str(found.capacity),
        "multiplier": str(found.multiplier),
        "register_label": config.REGISTER_LABELS.get(register, register),
    })
    if reading is None:
        out.update(ok=False, code="Q_MISSING_READING", detail="读数是空值哨兵")
    return out


def usage_quantize(value: Decimal) -> Decimal:
    return value.quantize(config.USAGE_QUANT)
