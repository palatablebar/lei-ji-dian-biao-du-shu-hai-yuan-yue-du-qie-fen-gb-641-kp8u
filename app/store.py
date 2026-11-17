"""SQLite 存储：批次、表计契约、读数、区间、月度分摊、账单、隔离、阶段、合计、清单。

约定：
- 用量与金额一律 TEXT（Decimal 字符串），不落 float；
- 一次运行的所有行在同一个事务里「先删后插」，失败整体回滚，不留半批数据；
- 页面永远只展示一个批次，按 batch_id 过滤。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    batch_key TEXT NOT NULL,
    run_id TEXT NOT NULL,
    sample TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    input_digest TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    note TEXT NOT NULL DEFAULT '',
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_batches_key ON batches(batch_key, status);
CREATE TABLE IF NOT EXISTS meters (
    batch_id TEXT NOT NULL, meter_id TEXT NOT NULL, name TEXT NOT NULL,
    capacity TEXT NOT NULL, multiplier TEXT NOT NULL, unit TEXT NOT NULL,
    registers TEXT NOT NULL, replaced_at TEXT, replaced_by TEXT, tariff TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS readings (
    batch_id TEXT NOT NULL, lineno INTEGER NOT NULL, segment INTEGER NOT NULL,
    meter_id TEXT NOT NULL, register TEXT NOT NULL, read_at TEXT NOT NULL,
    reading_text TEXT NOT NULL, unit_raw TEXT NOT NULL, reading_norm TEXT,
    unit_norm TEXT, read_type TEXT NOT NULL, status TEXT NOT NULL,
    code TEXT, flags_json TEXT NOT NULL DEFAULT '[]', note TEXT NOT NULL DEFAULT '',
    source_meter_id TEXT NOT NULL DEFAULT '', identity TEXT NOT NULL DEFAULT 'exact',
    identity_basis TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS intervals (
    batch_id TEXT NOT NULL, interval_id TEXT NOT NULL, meter_id TEXT NOT NULL,
    register TEXT NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL,
    prev_reading TEXT NOT NULL, cur_reading TEXT NOT NULL, delta TEXT NOT NULL,
    multiplier TEXT NOT NULL, unit TEXT NOT NULL, usage_kwh TEXT NOT NULL,
    days TEXT NOT NULL, method TEXT NOT NULL, flags_json TEXT NOT NULL DEFAULT '[]',
    source_meter_ids_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS allocations (
    batch_id TEXT NOT NULL, month TEXT NOT NULL, meter_id TEXT NOT NULL,
    register TEXT NOT NULL, usage_kwh TEXT NOT NULL, exact_share TEXT NOT NULL,
    tou_adjusted INTEGER NOT NULL DEFAULT 0, sources_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS bills (
    batch_id TEXT NOT NULL, month TEXT NOT NULL, meter_id TEXT NOT NULL,
    usage_kwh TEXT NOT NULL, amount_yuan TEXT NOT NULL,
    components_json TEXT NOT NULL, adjust_cents INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS quarantine (
    batch_id TEXT NOT NULL, lineno INTEGER NOT NULL, segment INTEGER NOT NULL,
    code TEXT NOT NULL, stage TEXT NOT NULL, level TEXT NOT NULL,
    field TEXT, raw_value TEXT, meter_id TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS pending (
    batch_id TEXT NOT NULL, lineno INTEGER NOT NULL, segment INTEGER NOT NULL,
    source_meter_id TEXT NOT NULL, candidate_id TEXT NOT NULL,
    candidates_json TEXT NOT NULL DEFAULT '[]', register TEXT NOT NULL,
    read_at TEXT NOT NULL, reading_text TEXT NOT NULL, reading_norm TEXT,
    unit_raw TEXT NOT NULL DEFAULT '', unit_norm TEXT, read_type TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS stages (
    batch_id TEXT NOT NULL, stage TEXT NOT NULL, in_count INTEGER NOT NULL,
    out_count INTEGER NOT NULL, quarantine_count INTEGER NOT NULL,
    status TEXT NOT NULL, elapsed_ms INTEGER NOT NULL, note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS totals (
    batch_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manifests (
    batch_id TEXT PRIMARY KEY, json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publications (
    month TEXT NOT NULL, version INTEGER NOT NULL,
    status TEXT NOT NULL,             -- active / superseded
    batch_id TEXT NOT NULL, run_id TEXT NOT NULL,
    input_digest TEXT NOT NULL, alias_digest TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    published_at TEXT NOT NULL, superseded_at TEXT,
    snapshot_json TEXT NOT NULL,
    PRIMARY KEY (month, version)
);
CREATE INDEX IF NOT EXISTS idx_publications_month_status
    ON publications(month, status);
"""


def _dec(text):
    return None if text is None else Decimal(str(text))


class Store:
    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).resolve().parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            columns = {row["name"] for row in
                       conn.execute("PRAGMA table_info(batches)").fetchall()}
            if "updated_at" not in columns:
                conn.execute("ALTER TABLE batches ADD COLUMN updated_at TEXT")
            # 旧库补列：身份证据与区间来源都要求可空默认，不重算历史批次
            for table, column, ddl in (
                    ("readings", "source_meter_id",
                     "ALTER TABLE readings ADD COLUMN source_meter_id TEXT NOT NULL DEFAULT ''"),
                    ("readings", "identity",
                     "ALTER TABLE readings ADD COLUMN identity TEXT NOT NULL DEFAULT 'exact'"),
                    ("readings", "identity_basis",
                     "ALTER TABLE readings ADD COLUMN identity_basis TEXT NOT NULL DEFAULT ''"),
                    ("intervals", "source_meter_ids_json",
                     "ALTER TABLE intervals ADD COLUMN source_meter_ids_json "
                     "TEXT NOT NULL DEFAULT '[]'")):
                existing = {row["name"] for row in
                            conn.execute(f"PRAGMA table_info({table})").fetchall()}
                if column not in existing:
                    conn.execute(ddl)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---------- 批次 ----------
    def get_batch(self, batch_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM batches WHERE batch_id=?",
                               (batch_id,)).fetchone()
        return dict(row) if row else None

    def find_done_by_key(self, batch_key: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM batches WHERE batch_key=? AND status='done' "
                "ORDER BY created_at LIMIT 1", (batch_key,)).fetchone()
        return dict(row) if row else None

    def find_by_key(self, batch_key: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM batches WHERE batch_key=? ORDER BY created_at DESC LIMIT 1",
                (batch_key,)).fetchone()
        return dict(row) if row else None

    def latest_batch(self, status: str = "done") -> dict | None:
        with self.connect() as conn:
            order = "COALESCE(updated_at, created_at) DESC, rowid DESC"
            row = conn.execute(
                f"SELECT * FROM batches WHERE status=? ORDER BY {order} LIMIT 1",
                (status,)).fetchone()
            if row is None:
                row = conn.execute(
                    f"SELECT * FROM batches ORDER BY {order} LIMIT 1").fetchone()
        return dict(row) if row else None

    def list_batches(self, limit: int = 30) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM batches ORDER BY COALESCE(updated_at, created_at) DESC, "
                "rowid DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def save_attempt(self, *, batch: dict, stages: list[dict], note: str = "") -> None:
        """失败尝试：只写批次与阶段，不动已有批次的数据。"""
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO batches (batch_id,batch_key,run_id,sample,status,created_at,
                        finished_at,input_digest,rule_version,params_json,note,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(batch_id) DO UPDATE SET
                     status=excluded.status, finished_at=excluded.finished_at,
                     note=excluded.note, run_id=excluded.run_id,
                     updated_at=excluded.updated_at""",
                (batch["batch_id"], batch["batch_key"], batch["run_id"], batch["sample"],
                 batch["status"], batch["created_at"], batch.get("finished_at"),
                 batch["input_digest"], batch["rule_version"],
                 json.dumps(batch.get("params", {}), ensure_ascii=False),
                 note or batch.get("note", ""), batch.get("updated_at")))
            conn.execute("DELETE FROM stages WHERE batch_id=?", (batch["batch_id"],))
            for s in stages:
                conn.execute(
                    """INSERT INTO stages (batch_id,stage,in_count,out_count,
                        quarantine_count,status,elapsed_ms,note) VALUES (?,?,?,?,?,?,?,?)""",
                    (batch["batch_id"], s["stage"], s["in_count"], s["out_count"],
                     s["quarantine_count"], s["status"], s["elapsed_ms"],
                     s.get("note", "")))

    def touch_batch(self, batch_id: str, stamp: str) -> None:
        """命中已有批次时刷新「最近一次运行」，页面才会停在这次看的结果上。"""
        with self.connect() as conn:
            conn.execute("UPDATE batches SET updated_at=? WHERE batch_id=?",
                         (stamp, batch_id))

    # ---------- 一次运行整体落库 ----------
    def save_run(self, *, batch: dict, meters: list[dict], readings: list[dict],
                 intervals: list[dict], allocations: list[dict], bills: list[dict],
                 quarantine: list[dict], stages: list[dict], totals: dict,
                 manifest_json: dict, pending: list[dict] | None = None) -> None:
        b = batch["batch_id"]
        with self.connect() as conn:
            for table in ("meters", "readings", "intervals", "allocations", "bills",
                          "quarantine", "pending", "stages", "totals"):
                conn.execute(f"DELETE FROM {table} WHERE batch_id=?", (b,))
            for m in meters:
                conn.execute(
                    """INSERT INTO meters (batch_id,meter_id,name,capacity,multiplier,unit,
                        registers,replaced_at,replaced_by,tariff)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (b, m["meter_id"], m["name"], str(m["capacity"]),
                     str(m["multiplier"]), m["unit"], ";".join(m["registers"]),
                     m.get("replaced_at"), m.get("replaced_by"), m["tariff"]))
            for r in readings:
                conn.execute(
                    """INSERT INTO readings (batch_id,lineno,segment,meter_id,register,
                        read_at,reading_text,unit_raw,reading_norm,unit_norm,read_type,
                        status,code,flags_json,note,source_meter_id,identity,
                        identity_basis)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (b, r["lineno"], r["segment"], r["meter_id"], r["register"],
                     r["read_at"], r["reading_text"], r["unit_raw"],
                     None if r.get("reading_norm") is None else str(r["reading_norm"]),
                     r.get("unit_norm"), r["read_type"], r["status"], r.get("code"),
                     json.dumps(r.get("flags", []), ensure_ascii=False),
                     r.get("note", ""),
                     r.get("source_meter_id") or r["meter_id"],
                     r.get("identity") or "exact",
                     r.get("identity_basis") or ""))
            for i in intervals:
                conn.execute(
                    """INSERT INTO intervals (batch_id,interval_id,meter_id,register,
                        start_at,end_at,prev_reading,cur_reading,delta,multiplier,unit,
                        usage_kwh,days,method,flags_json,source_meter_ids_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (b, i["interval_id"], i["meter_id"], i["register"], i["start_at"],
                     i["end_at"], str(i["prev_reading"]), str(i["cur_reading"]),
                     str(i["delta"]), str(i["multiplier"]), i["unit"],
                     str(i["usage_kwh"]), str(i["days"]), i["method"],
                     json.dumps(i.get("flags", []), ensure_ascii=False),
                     json.dumps(i.get("source_meter_ids", []), ensure_ascii=False)))
            for a in allocations:
                conn.execute(
                    """INSERT INTO allocations (batch_id,month,meter_id,register,usage_kwh,
                        exact_share,tou_adjusted,sources_json)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (b, a["month"], a["meter_id"], a["register"], str(a["usage_kwh"]),
                     str(a["exact_share"]), 1 if a.get("tou_adjusted") else 0,
                     json.dumps(a.get("sources", []), ensure_ascii=False)))
            for bill in bills:
                conn.execute(
                    """INSERT INTO bills (batch_id,month,meter_id,usage_kwh,amount_yuan,
                        components_json,adjust_cents) VALUES (?,?,?,?,?,?,?)""",
                    (b, bill["month"], bill["meter_id"], str(bill["usage_kwh"]),
                     str(bill["amount_yuan"]),
                     json.dumps(bill["components"], ensure_ascii=False),
                     int(bill.get("adjust_cents", 0))))
            for q in quarantine:
                conn.execute(
                    """INSERT INTO quarantine (batch_id,lineno,segment,code,stage,level,
                        field,raw_value,meter_id,detail) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (b, q["lineno"], q.get("segment", 1), q["code"], q["stage"],
                     q["level"], q.get("field"), q.get("raw_value"),
                     q.get("meter_id"), q.get("detail")))
            for row in pending or []:
                conn.execute(
                    """INSERT INTO pending (batch_id,lineno,segment,source_meter_id,
                        candidate_id,candidates_json,register,read_at,reading_text,
                        reading_norm,unit_raw,unit_norm,read_type,note,detail)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (b, row["lineno"], row.get("segment", 1), row["source_meter_id"],
                     row["candidate_id"],
                     json.dumps(row.get("candidates", []), ensure_ascii=False),
                     row["register"], row["read_at"], row["reading_text"],
                     str(row["reading_norm"]), row.get("unit_raw") or "",
                     row.get("unit_norm"), row["read_type"], row.get("note", ""),
                     row.get("detail", "")))
            for s in stages:
                conn.execute(
                    """INSERT INTO stages (batch_id,stage,in_count,out_count,
                        quarantine_count,status,elapsed_ms,note) VALUES (?,?,?,?,?,?,?,?)""",
                    (b, s["stage"], s["in_count"], s["out_count"],
                     s["quarantine_count"], s["status"], s["elapsed_ms"],
                     s.get("note", "")))
            for key, value in totals.items():
                conn.execute("INSERT INTO totals (batch_id,key,value) VALUES (?,?,?)",
                             (b, key, str(value)))
            conn.execute(
                "INSERT INTO manifests (batch_id,json) VALUES (?,?) "
                "ON CONFLICT(batch_id) DO UPDATE SET json=excluded.json",
                (b, json.dumps(manifest_json, ensure_ascii=False)))
            conn.execute(
                """INSERT INTO batches (batch_id,batch_key,run_id,sample,status,created_at,
                        finished_at,input_digest,rule_version,params_json,note,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(batch_id) DO UPDATE SET
                     status=excluded.status, finished_at=excluded.finished_at,
                     run_id=excluded.run_id, note=excluded.note,
                     updated_at=excluded.updated_at""",
                (b, batch["batch_key"], batch["run_id"], batch["sample"],
                 batch["status"], batch["created_at"], batch.get("finished_at"),
                 batch["input_digest"], batch["rule_version"],
                 json.dumps(batch.get("params", {}), ensure_ascii=False),
                 batch.get("note", ""), batch.get("updated_at")))

    # ---------- 读取 ----------
    def load_meters(self, batch_id: str) -> list[dict]:
        rows = self._all("SELECT * FROM meters WHERE batch_id=? ORDER BY rowid", (batch_id,))
        for r in rows:
            r["capacity"] = _dec(r["capacity"])
            r["multiplier"] = _dec(r["multiplier"])
            r["registers"] = r["registers"].split(";")
        return rows

    def load_readings(self, batch_id: str, status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM readings WHERE batch_id=?"
        args: list = [batch_id]
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY lineno"
        rows = self._all(sql, tuple(args))
        for r in rows:
            r["flags"] = json.loads(r.pop("flags_json") or "[]")
            r["reading_norm"] = _dec(r["reading_norm"])
            r["raw_meter_id"] = r.get("source_meter_id") or r["meter_id"]
        return rows

    def load_pending(self, batch_id: str) -> list[dict]:
        """待补身份的行：不是有效读数，也不在隔离区，单列一区。"""
        rows = self._all(
            "SELECT * FROM pending WHERE batch_id=? ORDER BY source_meter_id, read_at",
            (batch_id,))
        for row in rows:
            row["candidates"] = json.loads(row.pop("candidates_json") or "[]")
            row["reading_norm"] = _dec(row["reading_norm"])
            row["meter_id"] = row["source_meter_id"]
            row["code"] = "Q_IDENTITY_PENDING"
        return rows

    def load_intervals(self, batch_id: str) -> list[dict]:
        rows = self._all(
            "SELECT * FROM intervals WHERE batch_id=? ORDER BY meter_id, register, start_at",
            (batch_id,))
        for r in rows:
            r["flags"] = json.loads(r.pop("flags_json") or "[]")
            r["source_meter_ids"] = json.loads(r.pop("source_meter_ids_json") or "[]")
            for key in ("prev_reading", "cur_reading", "delta", "multiplier", "usage_kwh",
                        "days"):
                r[key] = _dec(r[key])
        return rows

    def load_allocations(self, batch_id: str) -> list[dict]:
        rows = self._all(
            "SELECT * FROM allocations WHERE batch_id=? ORDER BY month, meter_id, register",
            (batch_id,))
        for r in rows:
            r["usage_kwh"] = _dec(r["usage_kwh"])
            r["exact_share"] = _dec(r["exact_share"])
            r["sources"] = json.loads(r.pop("sources_json") or "[]")
            r["tou_adjusted"] = bool(r["tou_adjusted"])
        return rows

    def load_bills(self, batch_id: str) -> list[dict]:
        rows = self._all("SELECT * FROM bills WHERE batch_id=? ORDER BY month, meter_id",
                         (batch_id,))
        for r in rows:
            r["usage_kwh"] = _dec(r["usage_kwh"])
            r["amount_yuan"] = _dec(r["amount_yuan"])
            r["components"] = json.loads(r.pop("components_json") or "[]")
        return rows

    def load_quarantine(self, batch_id: str) -> list[dict]:
        return self._all(
            "SELECT * FROM quarantine WHERE batch_id=? ORDER BY lineno", (batch_id,))

    def load_stages(self, batch_id: str) -> list[dict]:
        from . import config
        rows = self._all("SELECT * FROM stages WHERE batch_id=? ORDER BY rowid", (batch_id,))
        for row in rows:
            row["label"] = config.STAGE_LABELS.get(row["stage"], row["stage"])
        return rows

    def load_totals(self, batch_id: str) -> dict:
        rows = self._all("SELECT key, value FROM totals WHERE batch_id=?", (batch_id,))
        return {r["key"]: r["value"] for r in rows}

    def load_manifest(self, batch_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT json FROM manifests WHERE batch_id=?",
                               (batch_id,)).fetchone()
        return json.loads(row["json"]) if row else None

    def count_rows(self, batch_id: str) -> dict:
        tables = ("meters", "readings", "intervals", "allocations", "bills", "quarantine")
        with self.connect() as conn:
            counts = {t: conn.execute(
                f"SELECT COUNT(*) AS n FROM {t} WHERE batch_id=?", (batch_id,)
            ).fetchone()["n"] for t in tables}
            counts["pending"] = conn.execute(
                "SELECT COUNT(*) AS n FROM pending WHERE batch_id=?",
                (batch_id,)).fetchone()["n"]
        return counts

    # ---------- 发布快照（只追加，不覆盖） ----------
    def list_publications(self, month: str | None = None) -> list[dict]:
        sql = ("SELECT month,version,status,batch_id,run_id,input_digest,alias_digest,"
               "rule_version,snapshot_hash,published_at,superseded_at "
               "FROM publications")
        args: tuple = ()
        if month:
            sql += " WHERE month=?"
            args = (month,)
        sql += " ORDER BY month, version"
        return self._all(sql, args)

    def active_publication(self, month: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM publications WHERE month=? AND status='active' "
                "ORDER BY version DESC LIMIT 1", (month,)).fetchone()
        return dict(row) if row else None

    def publication(self, month: str, version: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM publications WHERE month=? AND version=?",
                (month, version)).fetchone()
        return dict(row) if row else None

    def latest_version(self, month: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT MAX(version) AS v FROM publications WHERE month=?",
                (month,)).fetchone()
        return int(row["v"] or 0)

    def publish_snapshot(self, snapshot: dict) -> dict:
        """发布 = 同一事务里「旧版置 superseded + 新版本追加」。

        任一步失败整体回滚，旧版仍是唯一 active 版本——发布中断不丢旧版。
        版本号只增不减，历史快照内容永远保留（原始表号与映射依据都在快照里）。
        """
        month = snapshot["month"]
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT 1 FROM publications WHERE month=? AND version=?",
                (month, snapshot["version"])).fetchone()
            if existing is not None:
                # 历史版本不可覆盖：同一版本号第二次写入直接失败并回滚
                raise ValueError(
                    f"发布版本 {month} v{snapshot['version']} 已存在，历史版本不可覆盖")
            conn.execute(
                "UPDATE publications SET status='superseded', superseded_at=? "
                "WHERE month=? AND status='active'",
                (snapshot["published_at"], month))
            conn.execute(
                """INSERT INTO publications (month,version,status,batch_id,run_id,
                        input_digest,alias_digest,rule_version,snapshot_hash,
                        published_at,superseded_at,snapshot_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (month, snapshot["version"], "active", snapshot["batch_id"],
                 snapshot["run_id"], snapshot["input_digest"],
                 snapshot["alias_digest"], snapshot["rule_version"],
                 snapshot["snapshot_hash"], snapshot["published_at"], None,
                 json.dumps(snapshot, ensure_ascii=False, default=str)))
        return snapshot

    def load_publication_snapshot(self, month: str, version: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT snapshot_json FROM publications WHERE month=? AND version=?",
                (month, version)).fetchone()
        return json.loads(row["snapshot_json"]) if row else None

    def _all(self, sql: str, args: tuple) -> list[dict]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
