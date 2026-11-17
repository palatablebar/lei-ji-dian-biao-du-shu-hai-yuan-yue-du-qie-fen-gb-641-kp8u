# 运行手册 meterback

## 1 启动

```bash
cd projects/gb-641
docker compose up -d --build
curl -sS http://localhost:18200/api/health
```

页面：<http://localhost:18200/>。首次打开没有批次，选样例点「开始处理」即可跑通。

容器内端口 8000，宿主 18200；SQLite 落在命名卷 `meterback-data` 的 `/data/meterback.sqlite`。

## 2 主流程

1. 总览页选 `readings_dirty.csv`（脏样例，13 类问题），点「开始处理」。
2. 阶段卡显示七段「进 / 出 / 隔离」；任一阶段失败可点该卡的「重试」。
3. 读数诊断页看逐行诊断与隔离区，点「隔离区 CSV」下载证据。
4. 区间还原页看翻转、换表末段、新表首读与标记，可点「重算区间」。
5. 月度分摊页看每月合计、账单与尾差，可点「重算分摊」。
6. 批次对账页看四账与运行清单；导出页下载六份文件（另加已发布版本）。

命令行同一套接口：

```bash
curl -sS -X POST http://localhost:18200/api/runs \
  -H 'Content-Type: application/json' \
  -d '{"sample":"readings_dirty.csv"}'
curl -sS http://localhost:18200/api/reconcile | python3 -m json.tool
curl -sS -o /tmp/monthly.csv http://localhost:18200/api/export/monthly.csv
```

## 3 常见操作

- **同输入重跑**：再点一次「开始处理」或 POST 同一 `sample`，命中批次键，返回同一 `batch_id`，行数与合计不变。
- **强制重算**：POST `/api/batches/{batch_id}/retry`，用同一个 `sample` 重跑并覆盖该批次的行。
- **换样例**：下拉框里其余样例分别是干净、GBK 中文表头、末行截断、空文件、仅表头、全空行、表头缺列、表号升级已确认归并、表号升级待补登记。
- **归并一块表的两个表号**：在样例目录新建 `<样例名>.aliases.csv`，一行一个别名：

  ```csv
  alias_id,canonical_id,basis,source,effective_at
  7,007,终端升级工单 SW-2026-03-01 第 4 条,营销系统迁移记录,2026-03-01
  ```

  `basis` 必填。改登记表会改变批次键，需要用「重跑」或重新 POST 生成新批次。
- **发布月度结果**：月度分摊页点「发布月度结果」，或
  `POST /api/publications {"batch_id":"mb-...","month":"2026-03"}`。
  只发布已完成且四账平衡的批次；每个月份独立版本，旧版留在导出页。
- **补登记后**：改登记表 → 重跑 → 待补区应清空 → 重新发布该月份（会产生 v2，v1 仍可下载）。
- **重跑测试**：`docker compose exec -T app python -m pytest -q`。
- **清库**：`docker compose down -v` 删除命名卷；下次启动自动建表。

## 4 失败排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 422 `E_EMPTY_FILE` | 样例文件 0 字节 | 选非空样例 |
| 422 `E_HEADER_ONLY` | 只有表头 | 补数据行 |
| 422 `E_ALL_BLANK` | 数据行全空 | 检查导出是否只带格式 |
| 422 `E_HEADER_INVALID` | 缺 `meter_id/register/read_at/reading` | 按列别名表改名 |
| 422 `E_CONTRACT_INVALID` | `meters.csv` 重复表号、零倍率、换表目标缺失 | 修契约 |
| 422 `E_ALIAS_INVALID` | 身份登记缺 `basis`、目标是别名、别名本身是独立表计、目标不在契约 | 修 `*.aliases.csv` |
| 409 `E_PUBLISH_NOT_READY` | 批次未完成（失败/运行中） | 先重试跑完 |
| 409 `E_PUBLISH_UNBALANCED` | 四账不平 | 先看差额，修数据后重跑 |
| 404 `E_PUBLISH_NOT_FOUND` | 该月份还没有发布版本 | 先在月度页发布 |
| 400 `E_SAMPLE_UNKNOWN` | 样例名不在白名单 | 用页面下拉框里的名字 |
| 404 `E_BATCH_NOT_FOUND` | 还没有批次就导出 | 先跑一次处理 |
| 四账「不平」 | 逻辑或数据被改 | 看差额字段与说明，逐账核对 |

日志：`docker compose logs -f app`（uvicorn 访问日志）。运行清单里的 `input_digest` 可用来确认两次运行读的是同一份输入。

## 5 复算

1. 从导出页取 `monthly.csv` 与 `manifest.json`；
2. `manifest.json` 的 `inputs[].sha256` 与样例文件比对，确认输入未变；
3. 逐月加总 `用量kWh` 应等于 `manifest.json` 的 `counters` 与 `accounts[1]`（区间账）两侧；
4. 账单 `金额元` 之和应等于 `accounts[3]` 两侧，分项之和等于账单额；
5. 发布版对账：`/api/export/published.csv?month=YYYY-MM` 的「正式」行用量之和
   应等于发布快照的 `month_usage_kwh`；「待补（未计入）」行不参与任何合计。
「待补身份」行的用量只是参考量：补登记依据后重跑，才转为正式行。

## 6 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `METERBACK_DB` | `/data/meterback.sqlite` | SQLite 路径 |
| `METERBACK_SAMPLE_DIR` | `/app/sample_data` | 样例目录 |

规则版本写在 `app/config.py` 的 `RULE_VERSION`，进入运行清单；改规则必须同时加测试，否则同输入重跑会出现两个批次。
