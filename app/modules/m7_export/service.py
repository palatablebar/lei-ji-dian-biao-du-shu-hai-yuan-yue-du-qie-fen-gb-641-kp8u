"""导出编排：四份文件都从已落库的数据生成，保证与页面同源。

月度 CSV 的「口径」三列让读者一眼分清：正式行、已确认归并的原表号与依据。
待补身份另有单独下载（`/api/export/pending.csv`），不混进月度正式结果。
"""
from __future__ import annotations

import json

from ... import errors, identity
from ..m2_contract import contract as contract_mod
from . import csv_out, evidence, manifest_out


def _context(settings, store, batch_id: str | None) -> tuple[dict, str]:
    batch = store.get_batch(batch_id) if batch_id else store.latest_batch()
    if batch is None:
        raise errors.NotFound("还没有批次可以导出", code="E_BATCH_NOT_FOUND")
    return batch, batch["batch_id"]


def _identity_block(store, batch_id: str) -> dict:
    manifest = store.load_manifest(batch_id) or {}
    return manifest.get("identity") or {}


def monthly_csv(settings, store, batch_id: str | None = None) -> tuple[str, str]:
    batch, ident = _context(settings, store, batch_id)
    contracts = contract_mod.load(settings)
    allocations = store.load_allocations(ident)
    bills = store.load_bills(ident)
    merged = _identity_block(store, ident).get("merged", [])
    merged_by_month = identity.merged_evidence(store.load_intervals(ident), merged)
    body = csv_out.render(evidence.MONTHLY_HEADER,
                          evidence.monthly_rows(allocations, bills, contracts,
                                                merged_by_month))
    return body, f"monthly-{ident}.csv"


def pending_csv(settings, store, batch_id: str | None = None) -> tuple[str, str]:
    _, ident = _context(settings, store, batch_id)
    body = csv_out.render(evidence.PENDING_HEADER,
                          evidence.pending_rows(store.load_pending(ident)))
    return body, f"pending-{ident}.csv"


def intervals_csv(settings, store, batch_id: str | None = None) -> tuple[str, str]:
    _, ident = _context(settings, store, batch_id)
    body = csv_out.render(evidence.INTERVAL_HEADER,
                          evidence.interval_rows(store.load_intervals(ident)))
    return body, f"intervals-{ident}.csv"


def quarantine_csv(settings, store, batch_id: str | None = None) -> tuple[str, str]:
    _, ident = _context(settings, store, batch_id)
    body = csv_out.render(evidence.QUARANTINE_HEADER,
                          evidence.quarantine_rows(store.load_quarantine(ident)))
    return body, f"quarantine-{ident}.csv"


def readings_csv(settings, store, batch_id: str | None = None) -> tuple[str, str]:
    _, ident = _context(settings, store, batch_id)
    body = csv_out.render(
        evidence.READING_HEADER,
        evidence.all_reading_rows(store.load_readings(ident),
                                  store.load_pending(ident)))
    return body, f"readings-{ident}.csv"


def manifest_json(settings, store, batch_id: str | None = None) -> tuple[str, str]:
    batch, ident = _context(settings, store, batch_id)
    payload = manifest_out.build(store, batch, store.load_manifest(ident))
    return manifest_out.render_json(payload), f"manifest-{ident}.json"


EXPORTS = {
    "monthly": ("月度分摊 CSV", monthly_csv, "text/csv; charset=utf-8"),
    "pending": ("待补身份 CSV", pending_csv, "text/csv; charset=utf-8"),
    "intervals": ("区间还原 CSV", intervals_csv, "text/csv; charset=utf-8"),
    "quarantine": ("隔离区 CSV", quarantine_csv, "text/csv; charset=utf-8"),
    "readings": ("读数证据 CSV", readings_csv, "text/csv; charset=utf-8"),
    "manifest": ("运行清单 JSON", manifest_json, "application/json; charset=utf-8"),
}


def page_context(settings, store, batch) -> dict:
    if batch is None:
        return {"files": [], "manifest_summary": [], "counts": {}, "identity": None}
    ident = batch["batch_id"]
    counts = store.count_rows(ident)
    identity_block = _identity_block(store, ident)
    files = [
        {"key": "monthly", "label": "月度分摊 CSV", "href": "/api/export/monthly.csv",
         "rows": counts["allocations"]},
        {"key": "pending", "label": "待补身份 CSV", "href": "/api/export/pending.csv",
         "rows": counts["pending"]},
        {"key": "intervals", "label": "区间还原 CSV", "href": "/api/export/intervals.csv",
         "rows": counts["intervals"]},
        {"key": "quarantine", "label": "隔离区 CSV", "href": "/api/export/quarantine.csv",
         "rows": counts["quarantine"]},
        {"key": "readings", "label": "读数证据 CSV", "href": "/api/export/readings.csv",
         "rows": counts["readings"]},
        {"key": "manifest", "label": "运行清单 JSON", "href": "/api/export/manifest.json",
         "rows": 1},
    ]
    return {
        "files": files,
        "manifest_summary": manifest_out.summary(
            manifest_out.build(store, batch, store.load_manifest(ident))),
        "counts": counts,
        "identity": identity_block,
    }
