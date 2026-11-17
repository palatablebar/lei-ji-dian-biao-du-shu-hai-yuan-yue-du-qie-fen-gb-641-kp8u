"""月度发布：把「已确认归并的正式结果」与「待补身份」分栏冻结成不可变快照。

三条硬约束：

1. **可发布的东西必须是已完成、四账平衡的批次**；未完成或不平一律拒绝，
   旧版本保持 active。
2. **发布只追加版本，不覆盖历史**。同一事务里先把旧版本置 `superseded`，
   再写新版本；任一步失败整体回滚——发布中断后旧版仍是唯一 active 版本。
3. **快照自包含**：原始表号、映射依据、区间来源、待补参考量都写进快照，
   之后无论登记表怎么改、批次怎么重跑，历史版本都能逐位复算。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from decimal import Decimal

from . import config, errors, identity
from .modules.m2_contract import contract as contract_mod


def _iso_now() -> str:
    return dt.datetime.now(config.UTC).isoformat()


VOLATILE = ("published_at", "version", "snapshot_hash", "idempotent")


def _snapshot_hash(snapshot: dict) -> str:
    """内容哈希不含发布时刻与版本号：结果没变，哈希就不变。"""
    payload = json.dumps(
        {key: value for key, value in snapshot.items() if key not in VOLATILE},
        ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_snapshot(settings, store, batch: dict, month: str) -> dict:
    """按批次 + 月份生成发布快照内容（不落库）。"""
    if batch["status"] != "done":
        raise errors.Conflict(
            f"批次 {batch['batch_id']} 状态为 {batch['status']}，不能发布",
            code="E_PUBLISH_NOT_READY",
            detail={"batch_id": batch["batch_id"], "status": batch["status"]})
    totals = store.load_totals(batch["batch_id"])
    reconcile = json.loads(totals.get("reconcile_json") or "null")
    if not reconcile or not reconcile.get("all_balanced"):
        raise errors.Conflict(
            f"批次 {batch['batch_id']} 四账不平，不能发布",
            code="E_PUBLISH_UNBALANCED",
            detail={"batch_id": batch["batch_id"],
                    "accounts": (reconcile or {}).get("accounts")})

    contracts = contract_mod.load(settings)
    allocations = [item for item in store.load_allocations(batch["batch_id"])
                   if item["month"] == month]
    bills = [item for item in store.load_bills(batch["batch_id"])
             if item["month"] == month]
    intervals = [item for item in store.load_intervals(batch["batch_id"])
                 if _touches_month(item, month)]
    readings = store.load_readings(batch["batch_id"])
    pending = store.load_pending(batch["batch_id"])

    manifest = store.load_manifest(batch["batch_id"]) or {}
    identity_block = manifest.get("identity") or {}
    pending_summary = identity.pending_summary(pending, contracts)
    pending_month = identity.pending_by_month(pending_summary)

    usage = sum((item["usage_kwh"] for item in allocations
                 if _is_primary(item, contracts)), Decimal("0"))
    amount = sum((bill["amount_yuan"] for bill in bills), Decimal("0"))
    month_merged = identity.merged_evidence(
        intervals, identity_block.get("merged")).get(month, [])
    month_pairs = {(item["meter_id"], item["source_meter_id"])
                   for item in month_merged}
    aliased_rows = [row for row in readings
                    if row.get("identity") == "aliased"
                    and (row["meter_id"], row.get("source_meter_id")) in month_pairs]
    pending_ref = pending_month.get(month)
    if not allocations and not bills and not pending_ref and not intervals:
        raise errors.NotFound(f"{month} 没有可发布的正式结果或待补部分",
                              code="E_PUBLISH_NOT_FOUND")

    snapshot = {
        "month": month,
        "batch_id": batch["batch_id"],
        "run_id": batch["run_id"],
        "sample": batch["sample"],
        "rule_version": batch["rule_version"],
        "input_digest": batch["input_digest"],
        "alias_digest": totals.get("alias_digest", ""),
        "published_at": _iso_now(),
        "month_usage_kwh": str(usage.quantize(config.USAGE_QUANT)),
        "month_bill_yuan": str(amount.quantize(config.MONEY_QUANT)),
        "allocation_count": len(allocations),
        "bill_count": len(bills),
        "accounts": (reconcile or {}).get("accounts", []),
        "all_balanced": (reconcile or {}).get("all_balanced"),
        "published": {
            "allocations": [_allocation_payload(item, contracts)
                            for item in allocations],
            "bills": [_bill_payload(item) for item in bills],
            "intervals": [_interval_payload(item) for item in intervals],
        },
        "identity": {
            "confirmed": month_merged,
            "confirmed_reading_rows": len(aliased_rows),
            "confirmed_source_meter_ids": sorted(
                {item["source_meter_id"] for item in month_merged}),
            "registry_digest": totals.get("alias_digest", ""),
            "pending_series": pending_summary,
            "pending_reading_rows": len(pending),
            "pending_in_totals": False,
        },        "pending": pending_ref or {
            "month": month, "reference_usage_kwh": "0.000", "series": [],
            "source_meter_ids": [], "included": False,
            "note": "参考用量，未计入正式用量与费用"},
        "note": ("正式结果只含已完成且四账平衡的批次；待补身份单列，"
                 "不计入正式用量与费用"),
    }
    snapshot["snapshot_hash"] = _snapshot_hash(snapshot)
    return snapshot


def publish(settings, store, batch_id: str, month: str) -> dict:
    batch = store.get_batch(batch_id)
    if batch is None:
        raise errors.NotFound(f"批次 {batch_id} 不存在", code="E_BATCH_NOT_FOUND")
    snapshot = build_snapshot(settings, store, batch, month)
    active = store.active_publication(month)
    if active is not None and active["snapshot_hash"] == snapshot["snapshot_hash"]:
        # 同一批读数重发不产生新版本：版本号只描述「结果变过」，不描述「点过按钮」
        existing = store.load_publication_snapshot(month, active["version"])
        existing["idempotent"] = True
        return existing
    version = store.latest_version(month) + 1
    snapshot["version"] = version
    snapshot["idempotent"] = False
    store.publish_snapshot(snapshot)
    return snapshot


def months_in_batch(store, batch_id: str, contracts=None) -> list[str]:
    """本月可发布的月份：正式分摊所在月 + 待补参考量所在月。

    只含待补的月份也要能发布：它的快照是「本月正式为 0、待补 N 行」，
    这样月底不会因为一块表没有依据就整月不落版本。待补月份取的是参考用量
    实际覆盖的月份，而不是读数所在月（边界读数不产生用量）。
    """
    months = {item["month"] for item in store.load_allocations(batch_id)}
    if contracts is not None:
        pending = identity.pending_summary(store.load_pending(batch_id), contracts)
        months.update(identity.pending_by_month(pending))
    return sorted(months)


def _read_month(stamp: str | None) -> str:
    if not stamp:
        return ""
    from .modules.m5_months import weights
    return weights.month_of(dt.datetime.fromisoformat(stamp))


def _touches_month(interval: dict, month: str) -> bool:
    from .modules.m5_months import weights

    slices = weights.month_slices(
        dt.datetime.fromisoformat(interval["start_at"]),
        dt.datetime.fromisoformat(interval["end_at"]))
    return any(key == month and weight > 0 for key, weight in slices)


def _is_primary(allocation: dict, contracts) -> bool:
    contract = contracts.get(allocation["meter_id"])
    return contract is not None and contract.primary_register == allocation["register"]


def _allocation_payload(allocation: dict, contracts) -> dict:
    return {
        "month": allocation["month"],
        "meter_id": allocation["meter_id"],
        "register": allocation["register"],
        "usage_kwh": str(allocation["usage_kwh"]),
        "exact_share": str(allocation["exact_share"]),
        "tou_adjusted": bool(allocation["tou_adjusted"]),
        "sources": list(allocation["sources"]),
        "primary": _is_primary(allocation, contracts),
    }


def _bill_payload(bill: dict) -> dict:
    return {
        "month": bill["month"], "meter_id": bill["meter_id"],
        "usage_kwh": str(bill["usage_kwh"]),
        "amount_yuan": str(bill["amount_yuan"]),
        "adjust_cents": bill["adjust_cents"],
        "components": bill["components"],
    }


def _interval_payload(interval: dict) -> dict:
    return {
        "interval_id": interval["interval_id"],
        "meter_id": interval["meter_id"],
        "register": interval["register"],
        "start_at": interval["start_at"], "end_at": interval["end_at"],
        "usage_kwh": str(interval["usage_kwh"]),
        "method": interval["method"],
        "flags": interval["flags"],
        "source_meter_ids": interval.get("source_meter_ids", []),
    }


def active_snapshot(store, month: str) -> dict | None:
    row = store.active_publication(month)
    if row is None:
        return None
    return store.load_publication_snapshot(month, row["version"])


def history(store, month: str | None = None) -> list[dict]:
    return store.list_publications(month)


def export_rows(snapshot: dict) -> list[list[str]]:
    """发布版月度 CSV：正式行在前，待补行在后且明确标「未计入」。

    正式行的参考用量列为空；待补行的正式用量与金额列都为空，参考量只出现在
    「参考用量kWh」列——这样对「用量kWh」「金额元」两列求和，永远不会把
    没有登记依据的量算进正式结果。
    """
    rows: list[list[str]] = []
    confirmed = snapshot["identity"]["confirmed"]
    for allocation in snapshot["published"]["allocations"]:
        merges = [item for item in confirmed
                  if item["meter_id"] == allocation["meter_id"]]
        primary = allocation.get("primary", False)
        sources = "、".join(item["source_meter_id"] for item in merges) if primary else ""
        basis = ("；".join(dict.fromkeys(item["basis"] for item in merges))
                 if primary else "")
        rows.append([
            snapshot["month"], allocation["meter_id"], allocation["register"],
            "正式", allocation["usage_kwh"], "", "", "",
            sources, basis, snapshot["version"], snapshot["batch_id"],
        ])
    for bill in snapshot["published"]["bills"]:
        rows.append([
            snapshot["month"], bill["meter_id"], "BILL", "正式", bill["usage_kwh"],
            bill["amount_yuan"], "", str(bill.get("adjust_cents") or ""),
            "", "", snapshot["version"], snapshot["batch_id"],
        ])
    pending = snapshot.get("pending") or {}
    if pending.get("source_meter_ids"):
        rows.append([
            snapshot["month"],
            "、".join(pending["source_meter_ids"]), "TOTAL", "待补（未计入）",
            "", "", pending["reference_usage_kwh"], "",
            "", "", snapshot["version"], snapshot["batch_id"],
        ])
    return rows


EXPORT_HEADER = ["月份", "表号", "寄存器", "口径", "用量kWh", "金额元",
                 "参考用量kWh", "尾差分", "归并原表号", "映射依据",
                 "发布版本", "批次"]
