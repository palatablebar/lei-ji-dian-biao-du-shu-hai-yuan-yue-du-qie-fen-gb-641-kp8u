# 数据定义 meterback

本文件定义每个数字、时间、键的含义。页面只显示字段名与数值，解释在这里。

## 1 读数文件列契约

| 规范列名 | 可接受别名 | 类型 | 单位 | 可空 | 说明 |
|---|---|---|---|---|---|
| `meter_id` | 表号 / 电能表 / 电表编号 / meter | 字符串 | — | 否 | 前导零保形，`007` ≠ `7` |
| `register` | 寄存器 / 分量 / 费率 | 枚举 | — | 否 | `TOTAL` 总 / `PEAK` 峰 / `FLAT` 平 / `VALLEY` 谷，也接受 总/峰/平/谷 |
| `read_at` | 抄表时间 / 抄见时间 / 时间 | 时间 | — | 否 | 带偏移按偏移换算；无偏移按 +08:00；纯日期取当日 00:00 |
| `reading` | 读数 / 示数 / 累计读数 | 十进制 | 见表内 `unit` | 否 | 累计示数，不是用量 |
| `unit` | 单位 / 计量单位 | 枚举 | — | 是 | `kWh` / `度` / `千瓦时` / `MWh` / `兆瓦时`；空则取契约单位 |
| `read_type` | 抄表类型 / 类型 / 方式 | 枚举 | — | 是 | `N`/`正常`/`实抄`、`E`/`估抄`/`估算`；空按 `N` |
| `note` | 备注 / 说明 | 字符串 | — | 是 | 原样保存，只作证据 |

`meter_id / register / read_at / reading` 四列缺失即整文件失败（`E_HEADER_INVALID`）。

## 2 表计契约（meters.csv）

### 2.1 表计身份登记（`<样例名>.aliases.csv`）

采集终端升级后同一块物理表可能从 `007` 写成 `7`。`007` 与 `7` 默认是两个字符串，
不会互相归并；只有在身份登记里写了**依据**的别名才允许归并。

| 列 | 含义 | 约束 |
|---|---|---|
| `alias_id` | 导出件里的原始表号（如 `7`） | 必填；不得等于 `canonical_id` |
| `canonical_id` | 归属的契约表号（如 `007`） | 必填；必须在 `meters.csv` 里 |
| `basis` | 登记依据（工单号、迁移记录、生效日期） | 必填，空值整批失败 `E_ALIAS_INVALID` |
| `source` | 依据来源 | 可空 |
| `effective_at` | 生效时间；填了之后，早于该时点的读数不按此依据归并，仍留待补 | 可空 |

校验规则（任一不满足 → `E_ALIAS_INVALID`，整批不落数据）：

1. `basis` 为空：拒绝。没有依据的映射就是猜测。
2. `alias_id` 本身是契约里的独立表计：拒绝，避免把两块不同的表并成一块。
3. `canonical_id` 本身也是别名（链式 A→B→C）：拒绝，一次登记不许穿透多块表。
4. 同一 `alias_id` 指向多个目标：拒绝。

登记表按**样例名**定位：`readings_alias.csv` → `readings_alias.aliases.csv`。
登记表内容进入 `input_digest` 与批次键，改依据即生成新批次，旧批次不被静默改写。

### 2.2 身份判定四种结果

| 判定 | 条件 | 去向 |
|---|---|---|
| `exact` | 表号就在契约里 | 正常读数 |
| `aliased` | 登记表里有带依据的别名 | 归并到规范表号；原表号写 `source_meter_id`，依据写 `identity_basis` |
| `pending` | 不在契约，但形如某只表号的补零写法，且**无登记依据** | 「待补身份」单列，不进正式用量与费用 |
| `unknown` | 以上都不是 | `Q_UNKNOWN_METER` 隔离 |

待补行仍然做单行校验（负数、超疑似目标满量），通过后落 `pending` 表，
原因码 `Q_IDENTITY_PENDING`（warn 级，**不进隔离区原因码分布**）。
待补行不作单调锚点、不产生区间；只给出「参考用量」供补登记后核对。

| 列 | 含义 | 约束 |
|---|---|---|
| `capacity` | 满量（寄存器最大值） | > 0；翻转判定的基准 |
| `multiplier` | 综合倍率 | > 0；区间用量 = 差值 × 倍率 |
| `unit` | 契约单位 | 只能是 `kWh` / `MWh`；读数先折到它 |
| `registers` | 寄存器组合，`;` 分隔 | 子集 of TOTAL/PEAK/FLAT/VALLEY |
| `replaced_at` | 换表时点 | 带偏移；与 `replaced_by` 成对出现 |
| `replaced_by` | 新表号 | 必须存在于契约 |

主寄存器：有 `TOTAL` 取 `TOTAL`，否则取第一个分量。区间账与月度账只统计主寄存器，避免与峰平谷重复计数。

## 3 数值与精度

- 用量：`Decimal`，单位 kWh，最小刻度 `0.001`；量化只发生在区间用量、月度分摊、分量闭合三处。
- 金额：`Decimal`，单位元，最小刻度 `0.01`；量化只发生在账单合计与分项补差两处。
- 电价：元/kWh，`prices.csv`（`TOTAL 0.65`、`PEAK 1.05`、`FLAT 0.65`、`VALLEY 0.32`）。
- 对外 JSON 与 CSV：用量三位小数、金额两位小数，全部以字符串输出，禁止 float 往返。

## 4 时间与分桶

- 入库一律 UTC（`+00:00`）；页面展示与月桶按 `Asia/Shanghai (+08:00)`。
- 区间：`[起, 止)`，允许跨月、跨年；上一段止点等于下一段起点。
- 月桶：`[月初 00:00, 下月初 00:00)`，按覆盖天数加权；天数用秒数 / 86400 折算，保留 9 位小数。
- 相邻区间不重不漏由「半开 + 首尾相接」保证，测试 `test_intervals_are_half_open_and_contiguous` 固定。

## 5 空值与三态

- 哨兵清单：`-`、`--`、`—`、`/`、`N/A`、`null`、`未知`、`无`、`暂无`、`待定`、`空`、`未抄`、空串。
- `null`（无值）、`0`（有效零读数）、空串（格式缺陷）语义不同：前两者走不同原因码，空串按哨兵处理。
- 文件级三态：0 字节 → `E_EMPTY_FILE`；仅表头 → `E_HEADER_ONLY`；数据行全空 → `E_ALL_BLANK`。

## 6 去重与冲突键

- 业务键：`(meter_id, register, read_at)`（时间已归一到 UTC）。
- 同键多行且读数完全一致 → 保留行号最小的一条，其余记 `K_DUPLICATE_IDENTICAL`。
- 同键多行且读数不同 → 两条都进隔离区（`Q_DUPLICATE_CONFLICT`），带各自原值，不做「后写覆盖」。
- 不使用全行哈希：备注列变化不代表读数变化。

## 7 判定规则

| 判定 | 条件 | 结果 |
|---|---|---|
| 常规 | 本次 ≥ 上次 | 差值 = 本次 − 上次 |
| 满量翻转 | 本次 < 上次 且 上次 ≥ 满量 × 0.9 且 补满量后差值 ≤ 满量 | 差值 = 满量 − 上次 + 本次 |
| 倒走 | 本次 < 上次 且 不满足翻转前提 | `Q_REVERSE_READING`，整行隔离 |
| 换表末段 | 旧表最后一条读数（≤ 换表时点） | 区间止点 = 换表时点 |
| 新表首读 | 新表第一条读数 | 区间 = [换表时点, 首读时间)，用量从 0 起算 |
| 估抄 | `read_type = E` | 不作锚点、不参与单调判定，落在区间内时给区间打 `K_ESTIMATE_SKIPPED` |

## 8 尾差处理

1. 月度切分：按天数权重得到精确份额，各自向下取整后，余数按小数部分从大到小逐份补 0.001，保证每段之和等于区间用量。
2. 分量闭合：同月「总表 −（峰+平+谷）」若不等于 0，差额并入当月用量最大的分量（并列取 峰 > 平 > 谷），并标 `K_TOU_ADJUSTED`。
3. 账单闭合：账单额 = 先合计再四舍五入；分项金额各自向下截尾，差额按小数部分从大到小逐分补到分项，补完分项之和等于账单额，差额记 `adjust_cents`。

## 9 原因码

文件级 `E_*`：`E_EMPTY_FILE`、`E_HEADER_ONLY`、`E_ALL_BLANK`、`E_HEADER_INVALID`、`E_DECODE`、`E_SAMPLE_UNKNOWN`、`E_CONTRACT_INVALID`、`E_ALIAS_INVALID`、`E_BATCH_NOT_FOUND`、`E_PUBLISH_NOT_READY`、`E_PUBLISH_UNBALANCED`、`E_PUBLISH_NOT_FOUND`。

行级 `Q_*`：`Q_COLUMN_COUNT`、`Q_TRUNCATED_ROW`、`Q_MISSING_READING`、`Q_BAD_READING`、`Q_BAD_TIME`、`Q_UNKNOWN_METER`、`Q_BAD_REGISTER`、`Q_BAD_UNIT`、`Q_BAD_READ_TYPE`、`Q_NEGATIVE_READING`、`Q_EXCEEDS_CAPACITY`、`Q_REVERSE_READING`、`Q_DUPLICATE_CONFLICT`、`Q_METER_RETIRED`、`Q_IDENTITY_PENDING`。

标记 `K_*`：`K_ROLLOVER`、`K_REPLACEMENT_FIRST`、`K_ESTIMATE_SKIPPED`、`K_CHARSET_FALLBACK`、`K_BOM_STRIPPED`、`K_SEGMENT_SPLIT`、`K_TRUNCATED_TAIL`、`K_TOU_ADJUSTED`、`K_BILL_ROUNDED`、`K_UNIT_ASSUMED`、`K_UNIT_CONVERTED`、`K_DUPLICATE_IDENTICAL`、`K_METER_ID_ALIASED`、`K_IDENTITY_MERGED`。

## 10 发布与口径

- 发布对象是**月份**：一个月份的「正式结果」= 已确认归并后的分摊与账单。
- 只有 `status=done` 且四账平衡的批次可发布；否则 `409 E_PUBLISH_NOT_READY` /
  `E_PUBLISH_UNBALANCED`，旧版本保持 active。
- 版本号单调递增；发布在同一事务里「旧版置 superseded + 新版本插入」，
  失败整体回滚。历史版本内容（含原始表号、映射依据、待补参考量）永久保留。
- 同一批次、同一内容重复发布不产生新版本（`idempotent=true`）。
- 发布版 CSV 的「口径」列：`正式` / `待补（未计入）`；归并信息在「归并原表号」「映射依据」两列。
- 待补行的「用量kWh」「金额元」永远为空，参考量只写在独立的「参考用量kWh」列。
  对正式用量/金额列求和，不会把没有登记依据的量算进去。

唯一来源：`app/reasons.py`；页面与导出的原因文案都从它取。
