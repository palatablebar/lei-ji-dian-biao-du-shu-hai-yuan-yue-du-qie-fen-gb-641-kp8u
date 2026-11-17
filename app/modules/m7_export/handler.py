"""M7 入口：月度 / 待补 / 区间 / 隔离 / 读数证据 / 运行清单，以及发布版下载。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from ... import errors, publish
from ...web import Ctx, get_ctx, render
from ..m2_contract import contract as contract_mod
from . import csv_out, service

router = APIRouter(tags=["M7 导出"])


def _response(body: str, filename: str, media_type: str) -> Response:
    return Response(content=body, media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/api/export/monthly.csv")
def export_monthly(batch_id: str | None = None, ctx: Ctx = Depends(get_ctx)) -> Response:
    body, filename = service.monthly_csv(ctx.settings, ctx.store, batch_id)
    return _response(body, filename, "text/csv; charset=utf-8")


@router.get("/api/export/pending.csv")
def export_pending(batch_id: str | None = None, ctx: Ctx = Depends(get_ctx)) -> Response:
    body, filename = service.pending_csv(ctx.settings, ctx.store, batch_id)
    return _response(body, filename, "text/csv; charset=utf-8")


@router.get("/api/export/intervals.csv")
def export_intervals(batch_id: str | None = None,
                     ctx: Ctx = Depends(get_ctx)) -> Response:
    body, filename = service.intervals_csv(ctx.settings, ctx.store, batch_id)
    return _response(body, filename, "text/csv; charset=utf-8")


@router.get("/api/export/quarantine.csv")
def export_quarantine(batch_id: str | None = None,
                      ctx: Ctx = Depends(get_ctx)) -> Response:
    body, filename = service.quarantine_csv(ctx.settings, ctx.store, batch_id)
    return _response(body, filename, "text/csv; charset=utf-8")


@router.get("/api/export/readings.csv")
def export_readings(batch_id: str | None = None,
                    ctx: Ctx = Depends(get_ctx)) -> Response:
    body, filename = service.readings_csv(ctx.settings, ctx.store, batch_id)
    return _response(body, filename, "text/csv; charset=utf-8")


@router.get("/api/export/manifest.json")
def export_manifest(batch_id: str | None = None,
                    ctx: Ctx = Depends(get_ctx)) -> Response:
    body, filename = service.manifest_json(ctx.settings, ctx.store, batch_id)
    return _response(body, filename, "application/json; charset=utf-8")


@router.get("/export")
def page_export(request: Request, batch_id: str | None = None,
                ctx: Ctx = Depends(get_ctx)):
    batch = ctx.current_batch(batch_id)
    context = service.page_context(ctx.settings, ctx.store, batch)
    months = (publish.months_in_batch(ctx.store, batch["batch_id"],
                                      contract_mod.load(ctx.settings))
              if batch else [])
    return render(request, "export.html", page="export", page_title="导出",
                  batch=batch, months=months,
                  publications=publish.history(ctx.store), **context)


@router.post("/api/publications")
def api_publish(payload: dict | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    body = payload or {}
    batch_id = body.get("batch_id") or (ctx.current_batch() or {}).get("batch_id")
    if not batch_id:
        raise errors.NotFound("还没有批次可以发布", code="E_BATCH_NOT_FOUND")
    batch = ctx.store.get_batch(batch_id)
    if batch is None:
        raise errors.NotFound(f"批次 {batch_id} 不存在", code="E_BATCH_NOT_FOUND")
    months = ([body["month"]] if body.get("month")
              else publish.months_in_batch(ctx.store, batch_id,
                                           contract_mod.load(ctx.settings)))
    results = [publish.publish(ctx.settings, ctx.store, batch_id, month)
               for month in months]
    return {"batch_id": batch_id, "published": [
        {"month": item["month"], "version": item["version"],
         "snapshot_hash": item["snapshot_hash"],
         "month_usage_kwh": item["month_usage_kwh"],
         "month_bill_yuan": item["month_bill_yuan"],
         "pending_reference_kwh": item["pending"]["reference_usage_kwh"],
         "pending_source_meter_ids": item["pending"]["source_meter_ids"],
         "confirmed_source_meter_ids": item["identity"]
         ["confirmed_source_meter_ids"]}
        for item in results]}


@router.get("/api/publications")
def api_publications(month: str | None = None,
                     ctx: Ctx = Depends(get_ctx)) -> dict:
    return {"publications": ctx.store.list_publications(month),
            "active": {row["month"]: row["version"]
                       for row in ctx.store.list_publications()
                       if row["status"] == "active"}}


@router.get("/api/export/published.csv")
def export_published(month: str, version: int | None = None,
                     ctx: Ctx = Depends(get_ctx)) -> Response:
    row = (ctx.store.publication(month, version) if version
           else ctx.store.active_publication(month))
    if row is None:
        raise errors.NotFound(f"{month} 没有发布版本", code="E_PUBLISH_NOT_FOUND")
    snapshot = ctx.store.load_publication_snapshot(month, row["version"])
    body = csv_out.render(publish.EXPORT_HEADER, publish.export_rows(snapshot))
    return _response(body, f"published-{month}-v{row['version']}.csv",
                     "text/csv; charset=utf-8")
