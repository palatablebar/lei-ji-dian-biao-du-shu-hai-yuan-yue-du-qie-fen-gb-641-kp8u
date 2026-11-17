# meterback · 累计电表读数还原与月度切分

抄表班的读数还原工具：把累计示数还原成区间用量，按日权重切到自然月，出月度电费，并对四账。

- 使用者：供电所抄表班（示例 8 只表，覆盖换表、满量翻转、估抄、单位混写）
- 输入：`sample_data/` 里的读数 CSV + 表计契约 `meters.csv` + 电价 `prices.csv`
- 输出：区间用量、月度分摊、月度账单、隔离区证据、运行清单

## 功能模块

| 模块 | 动作 | 入口 |
|---|---|---|
| M1 读数采信 | 编码回退、BOM、重复表头、列数、截断、空态三态 | `POST /api/readings/import`、`/readings` |
| M2 表计契约 | 容量、倍率、寄存器、单位、换表事件、电价、表计身份登记、单行试解析 | `GET /api/contract`、`POST /api/contract/preview` |
| M3 质量隔离 | 负数、倒走、同键冲突、未知表、超满量、换表后抄表、待补身份 | `GET /api/quarantine`、`GET /api/quarantine.csv` |
| M4 区间还原 | 满量翻转、换表两段、新表首读起算、估抄标记、归并区间来源 | `POST /api/intervals/rebuild`、`/intervals` |
| M5 月度切分 | 日权重、最大余额、三分量闭合、账单尾差、月度发布（版本化快照） | `POST /api/months/allocate`、`POST /api/publications`、`/months` |
| M6 批次与对账 | 全流程、批次历史、四账、失败重试 | `POST /api/runs`、`GET /api/reconcile`、`/`、`/batches` |
| M7 导出 | 月度 / 待补身份 / 区间 / 隔离 / 读数证据 / 运行清单 / 已发布版本 | `GET /api/export/*`、`/export` |

## 核心规则

1. 读数按 `(表号, 寄存器, UTC 时间)` 排序；区间用量 =（本次 − 上次）× 综合倍率。
2. 本次 < 上次时，只有「上次 ≥ 满量 × 0.9」才按满量翻转补容量，否则判倒走并隔离。
3. 换表把区间拆成两段：旧表到换表时点，新表首读从零起算。
4. 估抄行不作锚点、不参与单调判定，落在区间内时给区间打标记。
5. 区间用量按覆盖天数切到 `[月初, 下月初)`，余数用最大余额法分到 0.001 kWh。
6. 同月「总表 = 峰 + 平 + 谷」的残差并入最大分量；账单先合计再舍入，分项补差。
7. 表号身份只认**有依据的登记**：`7 → 007` 必须写在 `<样例名>.aliases.csv` 且带 `basis`；
   没有依据的补零写法只列「待补身份」，不进正式用量与费用。
8. 月度结果按月份发布，版本只增不减：正式结果与待补部分在页面和下载里分列；
   发布中断保留旧版，同内容重发不新增版本。

## 定义与已知不覆盖

字段、单位、时间、空值、去重键与尾差规则见 `docs/DATA-DICTIONARY.md`；不覆盖的部分列在 `docs/DEPTH.md` 末节。

## 启动

```bash
docker compose up -d --build      # 宿主 18200 → 容器 8000
curl -sS http://localhost:18200/api/health
```

打开 <http://localhost:18200/>，选样例点「开始处理」。

## 演示步骤

1. 总览页 → 样例 `readings_dirty.csv` → 开始处理。
2. 读数诊断页：60 行原始数据 → 45 有效 + 1 估抄 + 14 隔离。
3. 区间还原页：32 段，其中 1 段满量翻转（249.900 kWh）、换表拆两段（60 + 45）。
4. 月度分摊页：5 个月、16 张账单、合计 4499.900 kWh / 3261.96 元；
   表计身份区显示已确认归并 0 组、待补 1 组（表号 `7`，未计入）。
5. 批次对账页：四账全平；导出页下载六份文件，另可在月度页按月份发布。
6. 选 `readings_alias.csv`：`7 → 007` 有登记依据，两段序列归并为一条；
   选 `readings_alias_pending.csv`：同样的读数但没有登记表，`7` 全部停在待补区。

## 页面

| 路径 | 内容 |
|---|---|
| `/` | 阶段卡、合计、四账、最近批次 |
| `/readings` | 读数行、隔离区、原因码分布 |
| `/intervals` | 区间明细、换表事件、标记 |
| `/months` | 按月分组的分摊、账单、已确认归并、待补身份、发布 |
| `/batches` | 运行清单、四账、行数、批次历史 |
| `/export` | 六份导出、已发布版本、清单字段 |

## 接口

`POST /api/runs`、`POST /api/readings/import`、`GET /api/readings/{batch_id}`、`GET /api/contract`、`POST /api/contract/preview`、`GET /api/quarantine`、`GET /api/quarantine.csv`、`GET /api/reasons`、`POST /api/intervals/rebuild`、`POST /api/months/allocate`、`POST /api/publications`、`GET /api/publications`、`GET /api/batches`、`GET /api/reconcile`、`POST /api/batches/{batch_id}/retry`、`GET /api/health`、`GET /api/export/{monthly,pending,intervals,quarantine,readings}.csv`、`GET /api/export/published.csv?month=YYYY-MM[&version=N]`、`GET /api/export/manifest.json`。OpenAPI 在 `/docs`。

## 目录

```text
app/            main.py config.py errors.py reasons.py manifest.py store.py web.py identity.py publish.py
app/modules/    m1_ingest m2_contract m3_quality m4_intervals m5_months m6_batches m7_export
app/templates/  base.html + 6 页
app/static/     theme.css layout.css components.css app.js table.js pipeline.js
docs/           DEPTH.md DATA-DICTIONARY.md RUNBOOK.md
sample_data/    读数样例（脏 / 干净 / GBK / 截断 / 空态 / 表号归并 / 待补）、meters.csv、prices.csv、*.aliases.csv
tests/          conftest.py + 每模块一个测试文件 + test_e2e.py + test_invariants.py + test_identity_publish.py
```

## 测试

```bash
docker compose exec -T app python -m pytest -q     # 106 passed
```

规则版本：`RULE_VERSION`（`app/config.py`）。改规则要同步加测试，否则同一份输入会出现两个批次。
