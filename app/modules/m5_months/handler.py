"""M5 入口：从当前批次区间重算月度分摊与账单，页面按月份组展示。

月度页面同时回答两个问题：正式结果里哪些是**已确认归并**，以及哪些原始表号
因为**没有登记依据**仍停在待补区、不进入本月用量与费用。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ... import identity, publish
from ...web import Ctx, get_ctx, render
from ..m2_contract import contract as contract_mod
from ..m6_batches import resume as resume_mod
from . import service

router = APIRouter(tags=["M5 月度切分"])


@router.post("/api/months/allocate")
def api_allocate(payload: dict | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    batch_id = (payload or {}).get("batch_id")
    batch = resume_mod.require_batch(
        ctx.store, batch_id or (ctx.current_batch() or {}).get("batch_id", ""))
    result = service.run(ctx.settings, ctx.store, batch["batch_id"])
    body = service.payload(result["allocations"], result["bills"],
                           service.primary_map(ctx.settings))
    body["batch_id"] = result["batch_id"]
    body["stats"] = {key: str(value) if hasattr(value, "as_tuple") else value
                     for key, value in result["stats"].items()}
    return body


@router.get("/months")
def page_months(request: Request, batch_id: str | None = None,
                ctx: Ctx = Depends(get_ctx)):
    batch = ctx.current_batch(batch_id)
    groups: list[dict] = []
    bills: list[dict] = []
    adjustments = 0
    merged: list[dict] = []
    pending: list[dict] = []
    pending_months: dict = {}
    publications: dict = {}
    if batch is not None:
        ident = batch["batch_id"]
        allocations = ctx.store.load_allocations(ident)
        bills = ctx.store.load_bills(ident)
        groups = service.rows_by_month(allocations, service.primary_map(ctx.settings))
        adjustments = sum(1 for item in allocations if item["tou_adjusted"])
        contracts = contract_mod.load(ctx.settings)
        manifest = ctx.store.load_manifest(ident) or {}
        merged = (manifest.get("identity") or {}).get("merged") or []
        merged_by_month = identity.merged_evidence(
            ctx.store.load_intervals(ident), merged)
        pending_rows = ctx.store.load_pending(ident)
        pending = identity.pending_summary(pending_rows, contracts)
        pending_months = identity.pending_by_month(pending)
        publications = {row["month"]: row["version"] for row in
                         ctx.store.list_publications() if row["status"] == "active"}
        for group in groups:
            group["merged"] = merged_by_month.get(group["month"], [])
            group["pending"] = pending_months.get(group["month"])
            group["published_version"] = publications.get(group["month"])
    return render(request, "months.html", page="months", page_title="月度分摊",
                  batch=batch, groups=groups, bills=bills, adjustments=adjustments,
                  merged=merged, pending=pending, publications=publications,
                  months=publish.months_in_batch(ctx.store, batch["batch_id"],
                                                 contract_mod.load(ctx.settings))
                  if batch else [])
