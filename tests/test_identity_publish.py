"""表计身份：确认归并、待补身份、误并防护、发布快照与重跑幂等。

覆盖的核心判断：
- 有登记依据的别名（7 → 007）才归并，原始表号与依据全链路可追；
- 没有依据的纯数字表号只列「待补」，绝不按补零规则擅自并入正式结果；
- 别名本身是契约表计时拒绝归并，防止把不同表计并成一块；
- 发布只追加版本，中断保留旧版；同一批读数重跑不新增用量、费用或版本。
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app import config, errors, identity, publish
from app.modules.m1_ingest import service as m1_service
from app.modules.m2_contract import contract as contract_mod
from app.modules.m2_contract import service as m2_service
from app.modules.m6_batches import service as batch_service


def _settings_with(tmp_path, **overrides):
    return config.Settings(db_path=str(tmp_path / "meterback.sqlite"),
                           sample_dir=str(config.PROJECT_ROOT / "sample_data"),
                           **overrides)


# ---------- 登记表校验：误并防护 ----------

def test_alias_without_basis_rejected(settings):
    """没有登记依据的别名不许进登记表——这正是「猜」和「确认」的边界。"""
    contracts = contract_mod.load(settings)
    original = identity.registry_path
    path = settings.sample_dir + "/__bad.aliases.csv"
    import pathlib
    pathlib.Path(path).write_text("alias_id,canonical_id,basis\n7,007,\n",
                                  encoding="utf-8")
    try:
        identity.registry_path = lambda _settings, _sample: pathlib.Path(path)
        with pytest.raises(errors.ContractInvalid) as excinfo:
            identity.load_registry(settings, "readings_alias.csv", contracts)
        assert excinfo.value.code == "E_ALIAS_INVALID"
    finally:
        identity.registry_path = original
        pathlib.Path(path).unlink()


def test_alias_cannot_target_another_alias(settings):
    """链式归并会把一次登记穿透多块表，直接拒绝。"""
    contracts = contract_mod.load(settings)
    import pathlib
    path = pathlib.Path(settings.sample_dir) / "__chain.aliases.csv"
    path.write_text("alias_id,canonical_id,basis\n"
                    "7,007,工单 A\n007,M0008,工单 B\n", encoding="utf-8")
    original = identity.registry_path
    try:
        identity.registry_path = lambda _settings, _sample: path
        with pytest.raises(errors.ContractInvalid):
            identity.load_registry(settings, "readings_alias.csv", contracts)
    finally:
        identity.registry_path = original
        path.unlink()


def test_alias_to_independent_contract_meter_rejected(settings):
    """别名本身已经是契约里的独立物理表：拒绝归并，避免把两块表并成一块。"""
    contracts = contract_mod.load(settings)
    import pathlib
    path = pathlib.Path(settings.sample_dir) / "__collide.aliases.csv"
    # M0003 是独立表计（抽水站），不是 M0008 的别名
    path.write_text("alias_id,canonical_id,basis\nM0003,M0008,错误登记\n",
                    encoding="utf-8")
    original = identity.registry_path
    try:
        identity.registry_path = lambda _settings, _sample: path
        with pytest.raises(errors.ContractInvalid) as excinfo:
            identity.load_registry(settings, "readings_alias.csv", contracts)
        assert "误并" in excinfo.value.message
    finally:
        identity.registry_path = original
        path.unlink()


def test_zero_pad_candidates_are_only_suspects():
    contracts = {"007": object(), "7A": object(), "M0008": object()}
    assert identity.zero_pad_candidates("7", contracts) == ["007"]
    assert identity.zero_pad_candidates("007", contracts) == []
    assert identity.zero_pad_candidates("7A", contracts) == []
    assert identity.zero_pad_candidates("0007", contracts) == ["007"]


# ---------- 有依据：归并成一条连续序列 ----------

def test_confirmed_alias_merges_into_one_series(settings, store):
    result = batch_service.run(settings, store, sample="readings_alias.csv")
    ident = result["identity"]
    assert ident["pending_count"] == 0
    assert ident["merged"] == [{
        "source_meter_id": "7", "meter_id": "007",
        "basis": "终端升级工单 SW-2026-03-01 第 4 条：表号 7 即七号井 007",
        "readings": 4, "months": ["2026-03", "2026-04", "2026-05"],
        "first_read_at": "2026-02-28T16:00:00+00:00",
        "last_read_at": "2026-04-30T16:00:00+00:00",
        "month_count": 3,
    }]
    # 升级前后是一条连续序列：1000 → 1300 → 1550 → 1900 → 2050 → 2450
    intervals = [item for item in store.load_intervals(result["batch"]["batch_id"])
                 if item["meter_id"] == "007"]
    usage = sum(item["usage_kwh"] for item in intervals)
    assert usage == Decimal("1450.000")
    assert all(item["usage_kwh"] > 0 for item in intervals)
    assert any("K_IDENTITY_MERGED" in item["flags"] for item in intervals)
    # 每段都能追回原始表号
    assert any("007" in item["source_meter_ids"] and "7" in item["source_meter_ids"]
               for item in intervals)


def test_alias_reading_keeps_source_id_and_basis(settings, store):
    result = batch_service.run(settings, store, sample="readings_alias.csv")
    rows = store.load_readings(result["batch"]["batch_id"], status="valid")
    aliased = [row for row in rows if row["identity"] == "aliased"]
    assert len(aliased) == 4
    assert {row["source_meter_id"] for row in aliased} == {"7"}
    assert {row["meter_id"] for row in aliased} == {"007"}
    assert all("SW-2026-03-01" in row["identity_basis"] for row in aliased)


def test_alias_digest_changes_batch_key(settings, store):
    """登记表是输入：改了依据就应当生成新批次，而不是静默改写旧批次。"""
    aliased = batch_service.run(settings, store, sample="readings_alias.csv")
    plain = batch_service.run(settings, store, sample="readings_alias_pending.csv")
    assert aliased["batch"]["batch_key"] != plain["batch"]["batch_key"]
    assert Decimal(aliased["totals"]["interval_usage"]) > \
        Decimal(plain["totals"]["interval_usage"])


# ---------- 无依据：待补，不进正式结果 ----------

def test_pending_identity_excluded_from_published_totals(settings, store):
    result = batch_service.run(settings, store, sample="readings_alias_pending.csv")
    totals = result["totals"]
    assert totals["pending"] == 2
    assert totals["valid"] == 6
    # 正式用量只来自 007（1000→1300）与 M0008（400+350+400），不含待补的 7
    assert Decimal(totals["interval_usage"]) == Decimal("1450.000")
    assert Decimal(totals["month_usage"]) == Decimal("1450.000")
    assert Decimal(totals["bill_amount"]) == Decimal("942.50")
    ident = result["identity"]
    assert ident["pending_count"] == 2
    assert ident["merged"] == []
    series = ident["pending"][0]
    assert series["source_meter_id"] == "7"
    assert series["candidates"] == ["007"]
    assert series["basis"] == ""
    assert Decimal(series["reference_usage_kwh"]) == Decimal("350.000")
    assert ident["pending_in_totals"] is False


def test_pending_rows_are_neither_valid_nor_quarantined(settings, store):
    result = batch_service.run(settings, store, sample="readings_alias_pending.csv")
    batch_id = result["batch"]["batch_id"]
    assert store.load_readings(batch_id, status="valid") != []
    pending = store.load_pending(batch_id)
    assert len(pending) == 2
    assert all(row["code"] == "Q_IDENTITY_PENDING" for row in pending)
    assert all(row["meter_id"] == "7" for row in pending)
    quarantine = store.load_quarantine(batch_id)
    assert "Q_IDENTITY_PENDING" not in {row["code"] for row in quarantine}
    # 读数账仍然闭合：原始行 = 有效 + 估抄 + 隔离 + 待补 + 空行
    accounts = {item["key"]: item for item in result["reconciliation"]["accounts"]}
    assert accounts["readings"]["balanced"] is True
    assert "待补 2" in accounts["readings"]["note"]


def test_pending_rows_never_unlock_rollover(settings, store):
    """待补行不做锚点：它自己的序列不参与正式单调判定，也不产生区间。"""
    result = batch_service.run(settings, store, sample="readings_alias_pending.csv")
    intervals = store.load_intervals(result["batch"]["batch_id"])
    assert all(item["meter_id"] != "7" for item in intervals)


def test_dirty_sample_plain_seven_is_pending(settings):
    ingested = m1_service.ingest(settings, "readings_dirty.csv")
    contracts = contract_mod.load(settings)
    interpreted = m2_service.interpret_rows(ingested.rows, contracts)
    assert [row["source_meter_id"] for row in interpreted.pending] == ["7"]
    assert not any(row["meter_id"] == "7" for row in interpreted.records)


# ---------- 发布：版本化、中断保留旧版、重跑不新增 ----------

def test_publish_requires_done_and_balanced(settings, store):
    result = batch_service.run(settings, store, sample="readings_alias.csv")
    batch_id = result["batch"]["batch_id"]
    store.save_attempt(batch={**result["batch"], "status": "failed"},
                       stages=[], note="E_TEST")
    with pytest.raises(errors.Conflict) as excinfo:
        publish.publish(settings, store, batch_id, "2026-03")
    assert excinfo.value.code == "E_PUBLISH_NOT_READY"
    assert store.active_publication("2026-03") is None


def test_publish_is_versioned_and_supersedes(settings, store):
    aliased = batch_service.run(settings, store, sample="readings_alias.csv")
    pending = batch_service.run(settings, store, sample="readings_alias_pending.csv")
    first = publish.publish(settings, store, pending["batch"]["batch_id"], "2026-03")
    second = publish.publish(settings, store, aliased["batch"]["batch_id"], "2026-03")
    assert (first["version"], second["version"]) == (1, 2)
    history = store.list_publications("2026-03")
    assert [(row["version"], row["status"]) for row in history] == [
        (1, "superseded"), (2, "active")]
    assert store.active_publication("2026-03")["version"] == 2
    # 被替换的版本内容仍然完整可下载，快照不丢
    old = store.load_publication_snapshot("2026-03", 1)
    assert old["batch_id"] == pending["batch"]["batch_id"]
    assert old["identity"]["confirmed"] == []
    assert old["pending"]["source_meter_ids"] == ["7"]


def test_publish_interruption_keeps_old_version(settings, store, monkeypatch):
    aliased = batch_service.run(settings, store, sample="readings_alias.csv")
    pending = batch_service.run(settings, store, sample="readings_alias_pending.csv")
    publish.publish(settings, store, pending["batch"]["batch_id"], "2026-03")
    original = store.publish_snapshot

    def explode(_snapshot):
        raise RuntimeError("磁盘写入中断")

    monkeypatch.setattr(store, "publish_snapshot", explode)
    with pytest.raises(RuntimeError):
        publish.publish(settings, store, aliased["batch"]["batch_id"], "2026-03")
    monkeypatch.setattr(store, "publish_snapshot", original)
    active = store.active_publication("2026-03")
    assert active["version"] == 1
    assert active["batch_id"] == pending["batch"]["batch_id"]
    assert [row["version"] for row in store.list_publications("2026-03")] == [1]


def test_publish_transaction_rolls_back_on_insert_failure(settings, store):
    """真正的库级中断：旧版已被置 superseded，但新版本写入失败 → 整体回滚。"""
    result = batch_service.run(settings, store, sample="readings_alias.csv")
    batch_id = result["batch"]["batch_id"]
    snapshot = publish.publish(settings, store, batch_id, "2026-03")
    broken = dict(snapshot, version=snapshot["version"] + 1)
    broken.pop("run_id")  # 插入时缺列 → 抛错，发生在「旧版置 superseded」之后
    with pytest.raises(KeyError):
        store.publish_snapshot(broken)
    active = store.active_publication("2026-03")
    assert active["version"] == snapshot["version"]
    assert active["snapshot_hash"] == snapshot["snapshot_hash"]
    assert [row["status"] for row in store.list_publications("2026-03")] == ["active"]


def test_published_version_is_immutable(settings, store):
    """同一版本号不允许再写一次：历史快照不能被覆盖。"""
    result = batch_service.run(settings, store, sample="readings_alias.csv")
    snapshot = publish.publish(settings, store, result["batch"]["batch_id"], "2026-03")
    with pytest.raises(ValueError):
        store.publish_snapshot(dict(snapshot, snapshot_hash="篡改"))
    active = store.active_publication("2026-03")
    assert active["snapshot_hash"] == snapshot["snapshot_hash"]


def test_unbalanced_batch_cannot_supersede_published_version(settings, store):
    pending = batch_service.run(settings, store, sample="readings_alias_pending.csv")
    published = publish.publish(settings, store, pending["batch"]["batch_id"],
                                "2026-03")
    broken = batch_service.run(settings, store, sample="readings_alias.csv")
    # 人为把该批次改成不平，模拟上游数据出错
    totals = store.load_totals(broken["batch"]["batch_id"])
    reconcile = json.loads(totals["reconcile_json"])
    reconcile["all_balanced"] = False
    totals["reconcile_json"] = json.dumps(reconcile)
    with store.connect() as conn:
        conn.execute("UPDATE totals SET value=? WHERE batch_id=? AND key='reconcile_json'",
                     (totals["reconcile_json"], broken["batch"]["batch_id"]))
    with pytest.raises(errors.Conflict) as excinfo:
        publish.publish(settings, store, broken["batch"]["batch_id"], "2026-03")
    assert excinfo.value.code == "E_PUBLISH_UNBALANCED"
    assert store.active_publication("2026-03")["version"] == published["version"]


def test_same_readings_republish_does_not_add_version(settings, store):
    result = batch_service.run(settings, store, sample="readings_alias.csv")
    batch_id = result["batch"]["batch_id"]
    first = publish.publish(settings, store, batch_id, "2026-03")
    second = publish.publish(settings, store, batch_id, "2026-03")
    assert first["version"] == second["version"] == 1
    assert second["idempotent"] is True
    assert len(store.list_publications("2026-03")) == 1


def test_rerun_same_input_keeps_totals_and_versions(settings, store):
    result = batch_service.run(settings, store, sample="readings_alias.csv")
    batch_id = result["batch"]["batch_id"]
    publish.publish(settings, store, batch_id, "2026-03")
    before_usage = store.load_totals(batch_id)["interval_usage"]
    before_bill = store.load_totals(batch_id)["bill_amount"]
    before_rows = store.count_rows(batch_id)
    again = batch_service.run(settings, store, sample="readings_alias.csv")
    assert again["idempotent_replay"] is True
    assert store.load_totals(batch_id)["interval_usage"] == before_usage
    assert store.load_totals(batch_id)["bill_amount"] == before_bill
    assert store.count_rows(batch_id) == before_rows
    third = publish.publish(settings, store, batch_id, "2026-03")
    assert third["version"] == 1
    assert len(store.list_publications("2026-03")) == 1


def test_snapshot_records_original_ids_and_basis(settings, store):
    result = batch_service.run(settings, store, sample="readings_alias.csv")
    snapshot = publish.publish(settings, store, result["batch"]["batch_id"], "2026-03")
    confirmed = snapshot["identity"]["confirmed"]
    assert confirmed[0]["source_meter_id"] == "7"
    assert "SW-2026-03-01" in confirmed[0]["basis"]
    assert "7" in snapshot["identity"]["confirmed_source_meter_ids"]
    # 区间证据也带上原始表号
    for interval in snapshot["published"]["intervals"]:
        assert interval["source_meter_ids"]


def test_published_export_marks_pending_not_included(settings, store):
    pending = batch_service.run(settings, store, sample="readings_alias_pending.csv")
    snapshot = publish.publish(settings, store, pending["batch"]["batch_id"], "2026-03")
    rows = publish.export_rows(snapshot)
    formal = [row for row in rows if row[3] == "正式"]
    waiting = [row for row in rows if row[3].startswith("待补")]
    assert formal and waiting
    # 正式行：参考用量列必须为空
    assert all(row[6] == "" for row in formal)
    # 待补行：正式用量与金额列为空，参考量单独一列
    assert waiting[0][4] == "" and waiting[0][5] == ""
    assert waiting[0][6] == snapshot["pending"]["reference_usage_kwh"]
    # 对「用量kWh」「金额元」求和不会把参考量算进去
    usage_total = sum(Decimal(row[4]) for row in rows if row[4])
    amount_total = sum(Decimal(row[5]) for row in rows if row[5])
    assert usage_total == Decimal(snapshot["month_usage_kwh"]) + \
        Decimal(snapshot["month_usage_kwh"])  # 分摊行 + 账单行
    assert amount_total == Decimal(snapshot["month_bill_yuan"])


def test_publication_endpoints(client_with_run):
    run = client_with_run.post("/api/runs", json={"sample": "readings_alias.csv"}).json()
    response = client_with_run.post("/api/publications",
                                    json={"batch_id": run["batch_id"]})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert {item["month"] for item in payload["published"]} == {
        "2026-01", "2026-02", "2026-03", "2026-04"}
    listing = client_with_run.get("/api/publications").json()
    assert listing["active"]["2026-03"] == 1
    csv = client_with_run.get("/api/export/published.csv?month=2026-03")
    assert csv.status_code == 200
    assert "正式" in csv.text
    missing = client_with_run.get("/api/export/published.csv?month=2020-01")
    assert missing.status_code == 404
    assert missing.json()["error"] == "E_PUBLISH_NOT_FOUND"


def test_pending_csv_endpoint(client_with_run):
    client_with_run.post("/api/runs", json={"sample": "readings_alias_pending.csv"})
    body = client_with_run.get("/api/export/pending.csv").text
    assert body.startswith("﻿")
    lines = body.splitlines()
    assert lines[0].lstrip("﻿").startswith("月份,原始表号,疑似表号")
    assert any(line.startswith("2026-03,7,007") for line in lines)
    assert any("待补（未计入）" in line for line in lines)


def test_monthly_csv_carries_identity_columns(client_with_run):
    client_with_run.post("/api/runs", json={"sample": "readings_alias.csv"})
    body = client_with_run.get("/api/export/monthly.csv").text
    assert "口径" in body.splitlines()[0]
    row = next(line for line in body.splitlines()
               if line.startswith("2026-03,007,总,"))
    cells = row.split(",")
    assert cells[7] == "正式 · 含已确认归并"
    assert cells[8] == "7"
    assert "SW-2026-03-01" in row


def test_months_page_shows_identity_sections(client_with_run):
    client_with_run.post("/api/runs", json={"sample": "readings_alias.csv"})
    page = client_with_run.get("/months").text
    assert "表计身份" in page
    assert "已确认归并" in page
    assert "7" in page and "007" in page
    assert "SW-2026-03-01" in page


def test_months_page_separates_formal_and_pending(client_with_run):
    client_with_run.post("/api/runs", json={"sample": "readings_alias_pending.csv"})
    page = client_with_run.get("/months").text
    assert "待补身份（未计入本表合计）" in page
    # 待补的参考量出现在提示里，正式合计只来自 M0008
    assert "参考用量 350.000" in page
    assert "未计入正式用量与费用" in page


def test_publish_flow_end_to_end(client_with_run):
    run = client_with_run.post("/api/runs", json={"sample": "readings_alias.csv"}).json()
    published = client_with_run.post(
        "/api/publications",
        json={"batch_id": run["batch_id"], "month": "2026-03"}).json()
    assert published["published"][0]["version"] == 1

    page = client_with_run.get("/months").text
    assert "已发布 v1" in page
    assert "下载已发布 v1" in page

    export_page = client_with_run.get("/export").text
    assert "月度发布版本" in export_page
    assert "生效" in export_page

    csv = client_with_run.get(
        "/api/export/published.csv?month=2026-03").text
    assert "2026-03,007,TOTAL,正式,350.000" in csv
    assert "正式 · 含已确认归并" not in csv  # 发布 CSV 用「正式」+ 独立归并列
    assert "SW-2026-03-01" in csv
    header = csv.splitlines()[0].lstrip("﻿").split(",")
    assert "参考用量kWh" in header


def test_alias_effective_window_keeps_earlier_readings_pending(settings, store):
    """登记依据有生效时点：生效前的 `7` 不能拿升级后的依据追认，仍留待补。"""
    import pathlib
    import shutil
    sample_dir = pathlib.Path(settings.sample_dir) / "__window"
    sample_dir.mkdir()
    try:
        for name in ("readings_alias.csv", "meters.csv", "prices.csv"):
            shutil.copy(pathlib.Path(settings.sample_dir) / name, sample_dir / name)
        (sample_dir / "readings_alias.aliases.csv").write_text(
            "alias_id,canonical_id,basis,effective_at\n"
            "7,007,工单 SW-2026-03-01,2026-04-01\n", encoding="utf-8")
        scoped = config.Settings(db_path=settings.db_path, sample_dir=str(sample_dir))
        result = batch_service.run(scoped, store, sample="readings_alias.csv")
        # 3 月 1 日的 7 在生效前 → 待补；4 月起的 7 归并
        assert result["totals"]["pending"] == 1
        assert result["identity"]["merged"][0]["readings"] == 3
        pending = store.load_pending(result["batch"]["batch_id"])
        assert pending[0]["read_at"] == "2026-02-28T16:00:00+00:00"
        assert "早于登记生效时点" in pending[0]["detail"]
    finally:
        shutil.rmtree(sample_dir)


def test_readings_csv_lists_pending_status(client_with_run):
    client_with_run.post("/api/runs", json={"sample": "readings_alias_pending.csv"})
    body = client_with_run.get("/api/export/readings.csv").text
    pending_lines = [line for line in body.splitlines() if ",待补," in line]
    assert len(pending_lines) == 2
    assert all(",Q_IDENTITY_PENDING," in line for line in pending_lines)
