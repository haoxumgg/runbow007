# 表格解析 → 提醒规则 → 飞书群推送：完整提示词

> 用途：把"Excel 下载完成之后"的这段链路（解析表格 → 判定异常 → 去重 → 组装消息 → 推送飞书群）
> 交给另一个 AI 独立写出来。本文档是**提示词**，不是说明书：每一节都可以整段复制粘贴给模型。
>
> 边界：**上游**（Playwright 登录 TMS、创建导出任务、从下载中心取文件）不在范围内，
> 本链路的输入就是"一个已经落到磁盘的 .xls/.xlsx 文件 + 页面上看到的总条数"。

---

## 0. 怎么用这套提示词

三种用法，按需要选：

1. **一次性投喂**：把「第 1 节 主提示词」+「第 9 节 测试」+「附录 A/B/C/D」整段发给模型，让它一次写完。
   适合上下文窗口大的模型。
2. **分模块投喂**（推荐）：先发第 1 节建立全局认知，然后按 2→3→4→5→6→7 逐个模块发，
   每个模块写完就跑测试。适合要求代码质量稳定的场景。
3. **当验收清单**：模型写完后，用第 9 节和附录 B 逐条对答案。

**投喂时必须一起说的三句话**（否则模型一定会自作主张）：

- 所有中文文案**逐字复制**，包括全角括号「」、竖线 `｜`、破折号，不要"优化措辞"——
  飞书群里的人靠固定句式认消息，改一个字就是线上变更。
- 时间一律**北京时间**（Asia/Shanghai），Excel 里读出来的是**不带时区的裸时间**，
  比较前把 `now` 也降成裸时间，不要给 Excel 时间硬贴时区。
- **默认不发消息**。发送必须由显式开关打开，缺开关时只打日志。

---

## 1. 主提示词（Master Prompt）

```text
你是一名 Python 后端工程师。请实现一个"订单异常提醒"链路的后半段：读取一个已经下载好的
Excel 报表，判定四类业务异常，做跨轮次去重，把结果组装成一条飞书富文本消息并推送到指定群。

## 背景

上游是一个物流 TMS（运输管理系统）的订单导出。每小时会自动导出一次当月全量订单
（数量级 4000–5000 行），另外每天下午跑一次"今日到达"的口径。导出文件落到本地磁盘，
同时上游会把"页面分页控件上显示的总条数"一起传下来，用于校验导出是否完整。

接收方是一个飞书群，群里是运营和承运商对接人。他们要的是"这一轮新出现的问题订单"，
不是"当前所有问题订单"——所以去重是这个系统的核心，不是附属功能。

## 技术栈与硬约束

- Python >= 3.11，标准库优先。第三方依赖只允许：openpyxl（读 .xlsx）、xlrd>=2（读 .xls）、
  requests（调飞书 API）、PyYAML（读配置）。不要引入 pandas。
- 数据落地只用一个 SQLite 文件，不要引入 ORM，直接用标准库 sqlite3。
- 全部用 `from __future__ import annotations`，函数签名带完整类型注解。
- 数据结构用 `@dataclass(frozen=True, slots=True)`，不要用裸 dict 在模块之间传数据。
- 行宽 100，ruff 规则集 E,F,I,UP,B 必须通过。
- 面向用户的错误信息、日志、飞书文案一律中文；代码注释和 docstring 里，
  "为什么这么写"用中文写清楚，"这段在干嘛"用英文一句话即可。

## 模块划分（请严格按这个文件划分，不要合并）

| 文件 | 职责 | 绝对不能做的事 |
|---|---|---|
| `models.py` | 纯数据结构：Order / ParsedWorkbook / ReminderCandidate / RunResult | 任何 IO、任何业务判断 |
| `excel.py` | 读表 → 校验 → 产出 Order 列表 | 不认识任何业务规则 |
| `rules.py` | Order 列表 + 当前时间 → ReminderCandidate 列表 | 不碰数据库、不碰网络、不做去重 |
| `database.py` | SQLite：订单快照、事件状态机、发送记录 | 不组装文案 |
| `notifier.py` | ReminderCandidate 列表 → 飞书富文本；以及发送客户端 | 不做去重判断 |
| `pipeline.py` | 编排上面所有模块，处理开关与护栏 | 不写业务判断逻辑 |

关键设计原则：**规则引擎是纯函数**。同样的订单列表 + 同样的时间，必须永远得到同样的候选列表。
"这条要不要发"是数据库层的问题，不是规则层的问题。这条边界如果破了，系统就没法测了。

## 数据流

下载好的 Excel + 页面总数
  → excel.read_orders()      解析 + 校验，任何一步不对就抛 WorkbookValidationError
  → pipeline 行数护栏         和历史行数比，异常就拒绝处理（见模块 6）
  → database.upsert_orders()  订单快照落库
  → rules.evaluate()          产出候选（本轮所有命中项）
  → database.sync_candidates() 更新事件状态机：新命中 open，消失的置 resolved
  → database.should_send()    逐条过滤出"这一轮真的要发"的
  → notifier.format_combined() 组装成一条消息（不是每条规则一条）
  → notifier.FeishuClient.send() 推送
  → database.mark_sent()      记录发送时间和 message_id

## 交付物

1. 上表六个模块的完整实现。
2. pytest 测试，覆盖率不低于 75%，测试里不允许真的发网络请求（用假 session）。
3. 一个 YAML 配置样例文件，含全部可调参数和中文注释。

## 验收标准

- 同一个异常订单，连续跑 10 轮，只在第一轮发出（R3/R4 除外，见模块 3 的重复提醒规则）。
- 导出文件缺表头 / 订单号重复 / 行数与页面总数对不上时，整轮失败并且**不发任何消息**，
  而不是"发一条不完整的"。
- 不打开发送开关时，全流程照跑、日志照打、数据库照写，就是不调飞书。
```

---

## 2. 模块提示词 ①：表格解析（`excel.py` + `models.py`）

```text
实现 `read_orders(path, *, expected_ui_total=None, total_tolerance=0) -> ParsedWorkbook`。

## 输入

- `path`：.xls 或 .xlsx 文件路径。其他后缀直接报错。
- `expected_ui_total`：上游从页面分页控件读到的总条数，可能为 None（手工处理文件时）。
- `total_tolerance`：允许的条数漂移，默认 0。

## 读文件

- `.xlsx`：openpyxl，`load_workbook(path, read_only=True, data_only=True)`，取 `active` 工作表。
  必须 `data_only=True`，否则公式单元格读出来是公式字符串。读完显式 close。
- `.xls`：xlrd（xlrd 2.x 只支持 .xls，这是对的，不要换库）。`open_workbook(on_demand=True)`，
  取第 0 张表。日期单元格（`XL_CELL_DATE`）必须用 `xldate_as_datetime(value, workbook.datemode)`
  转换——xls 存的是浮点序列号，直接 str() 会得到 `45234.5` 这种垃圾。空白/空单元格转成 None。
  读完 `release_resources()`。
- 两条分支都返回同一个三元组：`(工作表名, 表头字符串列表, 数据行迭代器)`。
  第一行是表头，数据从第 2 行开始（行号从 2 起算，报错时要能对应用户在 Excel 里看到的行号）。
- 空文件（没有表头行 / nrows == 0）报错："Excel 是空文件"。

## 表头映射：用别名表，不要用列序号

导出模板的列顺序会变，列名偶尔也换说法，所以按**中文表头名**定位列，并且每个字段给一组别名，
命中任意一个即可。表头取值时先 strip()。

内部字段名 → 可接受的中文表头（按顺序匹配，先命中先用）：

    order_no             订单号 / 订单单号
    organization         所属组织 / 执行组织
    carrier              承运商名称
    departed_at          离厂时间(承运商提货时间) / 离厂时间
    wms_posted_at        WMS过账时间
    expected_arrival_at  预计到达时间
    transport_status     状态
    contract_status      合同状态
    box_count            总箱数
    actual_arrival_at    实际到达时间
    signed_at            签收时间
    is_delayed           是否延迟 / 是否延误
    delay_reason         延迟原因 / 延误原因
    carrier_sla_hours    承运商时效 / 配送时效
    electronic_signed_at 电子签签署时间
    detail_count         明细单总数 / 订单数量

注意 `departed_at` 的第一个别名带半角括号，是导出模板的原文，别"顺手改成全角"。

**必填字段**（缺任何一个直接抛错，错误信息里列出缺的中文表头，用 `/` 连接别名）：
order_no, departed_at, wms_posted_at, transport_status, contract_status,
box_count, is_delayed, delay_reason。

其余字段缺列不算错，对应值取 None。

## 逐行转换

- 整行全空则跳过（不算数据行，也不推进错误行号以外的状态）。
- 某行的列数可能少于表头数，越界按 None 处理，不要 IndexError。
- 文本：None/空白 → 空字符串；float 且是整数值 → 去掉小数点（`3.0` → `"3"`，
  因为订单号在 xls 里可能被读成 float）；其余 str() 后 strip()。
- 订单号为空 → 报错 "第 N 行订单号为空"。
- 时间：已经是 datetime 就去掉时区信息（`replace(tzinfo=None)`）；是 date 就补 00:00:00；
  是字符串就按这些格式依次尝试：
      %Y/%m/%d %H:%M:%S、%Y-%m-%d %H:%M:%S、%Y/%m/%d %H:%M、%Y-%m-%d %H:%M、
      %Y/%m/%d、%Y-%m-%d、%Y-%m-%d %H:%M:%S.%f
  全都失败 → 报错 "第 N 行 {字段中文名} 无法解析: {原文}"。空值 → None。
- 整数：空 → 0；否则 `int(float(value))`（Excel 里数字常常是 float）；转不动报错
  "第 N 行 {字段中文名} 不是数字: {原值}"。
- 布尔（是否延迟）：`是/true/1/yes/y` → True；`否/false/0/no/n/空` → False；
  其他值报错 "第 N 行 是否延迟 值未知: {原值}"。大小写不敏感。
- 延迟原因：空白字符串要归一成 None（后面规则要靠这个判断"没填原因"）。

## 整体校验

1. **订单号唯一**：出现重复立刻抛错 "订单号重复: {订单号}（第 N 行）"。
   订单号是所有去重的主键，重复了后面全乱。
2. **一条数据都没有** → 抛错 "Excel 没有订单数据"。
3. **和页面总数对齐**（`expected_ui_total` 非 None 时）：
   - `abs(唯一订单数 - 页面总数) > total_tolerance` → 抛错
     "页面显示 {X} 条，但 Excel 有 {Y} 个唯一订单"。
   - 在容差内但不为 0 → 只打 WARNING 继续跑。
     理由要写进注释：读页面总数和 TMS 真正生成导出文件之间隔了几分钟，
     期间订单还在增减，差几条是正常漂移，不该让整轮提醒失败。

## 输出

`ParsedWorkbook(path, sheet_name, headers: tuple[str, ...], orders: tuple[Order, ...])`，
带 `row_count` 和 `unique_order_count` 两个 property。

`Order` 字段：order_no, organization, carrier, departed_at, wms_posted_at,
expected_arrival_at, transport_status, contract_status, box_count, actual_arrival_at,
signed_at, is_delayed, delay_reason, carrier_sla_hours, electronic_signed_at,
detail_count, source_row。再给一个 `to_json_dict()`，datetime 转 ISO 字符串，用于落库。

所有解析和校验失败统一抛 `WorkbookValidationError(ValueError)`，不要抛裸 ValueError，
上层要能区分"表格有问题"和"程序有 bug"。
```

---

## 3. 模块提示词 ②：规则引擎（`rules.py`）

```text
实现 `RuleEngine(config).evaluate(orders, *, now, rule_codes=None) -> list[ReminderCandidate]`。

纯函数：不读数据库、不发网络、不看历史。同样输入必须同样输出。

`rule_codes` 是本轮想跑的规则；先大写归一，再和配置里 `enabled` 求交集——
命令行请求了但配置没开的规则，不跑。

`ReminderCandidate(event_key, rule_code, scenario, reason, order)`。
`event_key` 是跨轮次去重的唯一标识，**它的构造方式就是"什么算同一个问题"的定义**，
每条规则的 key 设计理由见下。

## 一个前置约定：在途状态有两种写法

TMS 里 "运输在途" 和 "运输在途（已离厂）" 是同一个业务状态的两种标签，判定时都要认。
写一个 `_is_in_transit(status)` 统一处理，strip 后比对，不要在四个地方各写各的。

## R1 WMS过账时效

判定：`departed_at is None` 且 `wms_posted_at is not None`
      且 `(now - wms_posted_at) > wms_lead_minutes 分钟`（默认 90，可配置）。

`now` 可能带时区（Asia/Shanghai），Excel 时间不带。比较前把 now 降成裸时间
（`now.replace(tzinfo=None)`），不要反过来给 Excel 时间贴时区。

- event_key: `R1|{订单号}|{WMS过账时间 ISO}`
  —— 带上过账时间，是因为 TMS 里过账可以被撤销重做；重做之后是一个**新问题**，应该重新提醒。
- scenario: `departure_missing_overdue`
- reason: `WMS过账已 {分钟数保留一位小数} 分钟，仍无离厂时间`

## R2 今日实际到达

判定：`actual_arrival_at` 不为空，且其**日期部分**等于 `now` 的日期，
      且运输状态是在途（用上面的 `_is_in_transit`）。

- event_key: `R2|{订单号}|{今天的日期 ISO}`
  —— 带日期，因为这是"每天一报"的口径，跨天就是新的一条。
- scenario: `arrival_today`
- reason: `实际到达日期为今天但运输状态仍为在途`

## R3 合同签署异常（两个场景）

共同前提：`actual_arrival_at` 和 `signed_at` 都不为空且**完全相等**。
（业务含义：签收时间是系统按到达时间自动带出来的，说明没有人真的做过签收动作。）

场景一 客户未完成电子签：运输状态 == "已签收" 且 合同状态 == "签署中"
- event_key: `R3|unsigned|{订单号}`
- scenario: `customer_unsigned`
- reason: `实际到达时间与签收时间一致，订单已签收但合同仍在签署中`

场景二 运营未操作签收：运输状态是在途 且 合同状态 == "已完成"
- event_key: `R3|operation_pending|{订单号}`
- scenario: `operation_pending`
- reason: `实际到达时间与签收时间一致，合同已完成但运输状态仍为在途`

两个场景互斥，命中一个就返回，都不命中返回 None。
key 里不带时间：这类异常会持续存在直到有人去处理，同一单就是同一个问题。

## R4 延迟无原因

判定：`is_delayed` 为 True，且 `delay_reason` 为空 / 空字符串 / 全是空白。

- event_key: `R4|{订单号}`
- scenario: `delay_reason_missing`
- reason: `是否延迟为是且延迟原因为空`

## 输出顺序

按订单遍历，每个订单内部按 R1→R2→R3→R4 判定。一个订单可以同时命中多条规则，
每条都产出独立的候选。不要去重、不要排序、不要截断。
```

---

## 4. 模块提示词 ③：状态机与去重（`database.py`）

```text
用标准库 sqlite3 实现 `SQLiteStore`。连接时 `PRAGMA foreign_keys = ON` 和
`PRAGMA journal_mode = WAL`，`row_factory = sqlite3.Row`。建表用 `CREATE TABLE IF NOT EXISTS`，
每次实例化都跑一遍建表脚本（幂等）。

## 表结构

runs(run_id PK, source_file, file_sha256, started_at, finished_at, status,
     row_count, candidate_count, sent_count, error)

orders(order_no PK, payload_json, source_file, last_seen_at)

reminder_events(event_key PK, rule_code, order_no, scenario, state,
                first_seen_at, last_seen_at, last_sent_at, resolved_at)
  索引：(rule_code, state)

deliveries(id PK AUTOINCREMENT, event_key FK, run_id FK, status, message_id, error, created_at)

## 需要的方法

- `begin_run(run_id, source_file, file_sha256, started_at)`：插一条 status='running'。
- `complete_run(run_id, *, finished_at, status, row_count=None, candidate_count=None,
   sent_count=None, error=None)`：成功和失败都要调，失败时写 error。
- `upsert_orders(orders, *, source_file, seen_at)`：executemany + ON CONFLICT DO UPDATE，
  payload 存 `json.dumps(order.to_json_dict(), ensure_ascii=False, separators=(",", ":"))`。
- `recent_successful_row_counts(limit=5) -> list[int]`：最近 N 次成功运行的行数，新的在前，
  只取 `status='success' AND row_count > 0`。给上层算中位数当基线用。
- `sync_candidates(...)`、`should_send(...)`、`mark_sent(...)`、`mark_failed(...)`：见下。

## sync_candidates —— 事件状态机（这是整个系统最容易写错的地方）

签名：
```
sync_candidates(candidates, *, selected_rules, observed_order_nos, seen_at,
                reopen_grace_hours=0)
```

两步：

**第一步，本轮命中的全部 upsert 成 open：**
- 新 key：插入，state='open'，first_seen_at = last_seen_at = seen_at，resolved_at = NULL。
- 已存在的 key：state 置回 'open'，刷新 last_seen_at，resolved_at 清空，
  `last_sent_at` 按下面这条规则决定是保留还是清空：

      仅当该事件此前处于 resolved 且 resolved_at <= (seen_at - reopen_grace_hours)
      时，才把 last_sent_at 清成 NULL；否则**保留原值**。

  这一条必须写注释解释：命中集合每小时都在抖动（同一订单这轮消失、下轮又出现，
  因为 TMS 数据在实时变）。如果一消失就清空发送记录，那么抖一次就会重复推送一次，
  群里会被刷屏。只有真的安静了 reopen_grace_hours（默认 12 小时）之后再复发，
  才算一个新问题，才允许重新提醒。

**第二步，本轮没命中但订单还在报表里的，置 resolved：**
对每条本轮跑过的规则（`selected_rules`），把满足下面条件的行改成
state='resolved', resolved_at=seen_at：
  rule_code = 该规则 AND state='open'
  AND order_no IN (本轮报表里出现过的所有订单号)
  AND event_key NOT IN (本轮该规则命中的 key)

`order_no IN (本轮观察到的订单)` 这个条件是**必须的**，不能省：报表是按当月口径导出的，
上个月的订单这个月不在报表里，它们的 open 事件应该保持原样，而不是因为"这轮没看见"
就被误判成已解决。

本轮该规则一个都没命中时，走同一条 SQL 去掉 `NOT IN` 那段即可。
`observed_order_nos` 为空时直接 return（不做任何 resolve）。

## should_send —— 单条候选要不要发

```
should_send(candidate, *, now, repeat_hour) -> bool
```
- 查不到该 event_key，或 `last_sent_at` 为空 → True（从没发过）。
- 已经发过：
  - R1、R2 → False。这两类是"一次性告知"，发过就不再重复。
  - R3、R4 → 只有当 `last_sent_at` 的日期 < 今天，**并且** `now.hour >= repeat_hour`
    （默认 9）时才 True。也就是"未解决的异常，每天上班后最多再提醒一次"。

## mark_sent / mark_failed

- `mark_sent(candidates, *, run_id, message_id, sent_at)`：逐条更新 last_sent_at，
  并往 deliveries 插一条 status='sent' 带 message_id。
- `mark_failed(candidates, *, run_id, error, failed_at)`：往 deliveries 插 status='failed' 带 error，
  **不要**更新 last_sent_at（没发出去就不算发过，下一轮要重试）。
```

---

## 5. 模块提示词 ④：消息编排（`notifier.MessageFormatter`）

```text
把候选列表变成一条飞书富文本（post）消息。

## 消息数据结构

```
@dataclass(frozen=True, slots=True)
class FeishuMessage:
    title: str
    content: list[list[dict[str, Any]]]   # 外层是行，内层是这一行的富文本片段
```
飞书 post 消息体的 content 就是"行的列表"，每行是"元素的列表"。
纯文本行写成 `[{"tag": "text", "text": "..."}]`，@ 某人写成 `{"tag": "at", "user_id": "ou_xxx"}`。

## 构造

`MessageFormatter(*, mention_user_id: str, mention_name: str)`。
`mention_user_id` 为空时不 @ 任何人，只发普通群消息（未配置 open_id 时乱填会发送失败）。

## 主入口：一轮只发一条消息

```
format_combined(rule_codes: tuple[str, ...],
                candidates: list[ReminderCandidate],
                *, current_candidates: list[ReminderCandidate] | None = None) -> FeishuMessage
```
- `candidates`：本轮**真的要发**的（已经过去重过滤）。
- `current_candidates`：本轮**当前所有命中**的（未过滤）。用来在消息里说明
  "还有几个是之前已经提醒过的"，让群里的人知道总盘子有多大。缺省时等于 candidates。
- `rule_codes` 里出现未知规则码 → 抛 ValueError("未知规则: ...")。

标题：四条规则全跑时是 `R1–R4订单提醒汇总`（注意是 en dash `–`，不是减号）；
否则把规则码用顿号连接，例如 `R1、R3、R4订单提醒汇总`。

正文结构（顺序严格按 rule_codes 给的顺序）：

第 1 行：@ 行。
  - 配了 mention_user_id：`[{"tag":"at","user_id":...}, {"tag":"text","text":" 请关注以下订单："}]`
    （text 前面有一个空格，把 @ 和文字分开）
  - 没配：`[{"tag":"text","text":"请关注以下订单："}]`

然后每条规则一段：
  段首行：`【{规则码}｜{规则标题}】`（竖线是全角 `｜`）
    规则标题固定为：
      R1 → WMS过账时效预警
      R2 → 今日签收提醒
      R3 → 合同签署状态异常提醒
      R4 → 延迟无原因提醒
  然后分三种情况：
  a) 本轮该规则有要发的：
     - 若 `当前命中数 > 要发数`，先加一行：
       `当前符合条件共 {当前命中数} 个订单；以下为本轮新增或到期重提醒的 {要发数} 个订单，另有 {差值} 个此前已提醒。`
     - 再加该规则的明细行（见下）。
  b) 本轮没有要发的、但当前仍有命中：
     `当前仍有 {当前命中数} 个符合条件订单；本轮无新增提醒（此前已提醒）。`
  c) 当前一个都没命中：
     `无符合条件订单。`

## 各规则的明细行（逐字复制）

R1：
  `共 {N} 单，离厂时间为空且WMS过账已超过配置阈值。`
  每条：`- {订单号}｜箱数 {总箱数}｜{候选的 reason}`

R2：
  `总共 {N} 个订单，总共 {箱数合计} 箱。`
  每条：`- {订单号}｜箱数 {总箱数}`

R3（按 scenario 分两组，任一组为空就整组不输出）：
  客户未电子签组：
    `【客户未电子签】`
    `总共 {N} 个订单，总共 {箱数合计} 箱。`
    每条：`- {订单号}｜箱数 {总箱数}`
  运营未操作签收组：
    `【运营未操作签收】`
    `提醒内容：共 {N} 个订单。`
    每条：`- {订单号}`
    收尾：`请运营人员将状态更新为「已签收」，合同状态为「已完成」。`

R4：
  `综合统计：共 {N} 个订单。`
  `明细：`
  每条：`- {订单号}`
  收尾：`请督促相关人员及时填写延误原因，确保延误订单有完整的归因记录。`

## 附带一个单规则入口

```
format(rule_code, candidates) -> FeishuMessage
```
标题用上面的规则标题，正文是 @ 行 + 该规则明细行。候选为空时抛 ValueError("没有可格式化的提醒")。
（当前生产路径走 format_combined，这个入口保留给单规则调试和测试。）

## 注意

- 不要做订单号截断、不要"超过 N 条就省略"。运营要的是全量清单，可以刷屏。
- 不要按订单号排序，保持规则引擎给出的顺序（即报表行序），方便和 Excel 对照。
```

---

## 6. 模块提示词 ⑤：飞书发送（`notifier.FeishuClient`）

```text
用 requests 实现飞书自建应用的群消息发送。

## 鉴权

POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal/
body: {"app_id": ..., "app_secret": ...}
成功响应含 `tenant_access_token` 和 `expire`（秒）。

token 要缓存在实例上，用 `time.monotonic()` 记过期时间，提前 300 秒过期
（`max(60, expire - 300)`），避免掐在边界上用到刚失效的 token。
响应非 2xx 或 `code != 0` → 抛 `FeishuError(f"获取飞书访问凭证失败: {code} {msg}")`。

**app_secret 不从配置文件读**，由调用方注入（Windows 走系统凭据管理器，
Linux 走权限 600 的 env 文件）。绝对不要把 secret 写进 YAML、日志或异常信息里。

## 发送

POST https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id
headers: Authorization: Bearer {token}，Content-Type: application/json; charset=utf-8
body:
```
{
  "receive_id": "<群 chat_id，形如 oc_xxx>",
  "msg_type": "post",
  "content": "<JSON 字符串>"
}
```
注意 `content` 是**字符串**，不是对象——要 `json.dumps({"zh_cn": {"title":..., "content":...}},
ensure_ascii=False)`。这是飞书 API 的坑，传对象会被拒。

## 重试

最多 3 次，间隔 0 / 1 / 3 秒（sleep 在请求前）。每次重试都重新取 token
（token 可能就是失效原因）。只对 429、500、502、503、504 重试；其他状态码或第 3 次失败就退出循环。
响应体解析不出 JSON 时，用 `{"code": status_code, "msg": response.text[:300]}` 兜底
（截断，避免把整页 HTML 塞进日志）。

成功（`response.ok and data["code"] == 0`）→ 返回 `data["data"]["message_id"]`。
失败 → 抛 `FeishuError(f"飞书发送失败: {code} {msg}")`。

构造函数要能注入 `session`（`requests.Session | None`），测试用假 session 顶掉，
测试里不允许出网。
```

---

## 7. 模块提示词 ⑥：编排（`pipeline.py`）

```text
实现 `Pipeline(config).process_file(...) -> RunResult`。

```
process_file(source_file, *, rule_codes=None, expected_ui_total=None,
             send=False, max_send_orders=None, force_send=False) -> RunResult
```

## 参数校验（在做任何事之前）

- 规则码大写归一；出现 R1–R4 之外的 → ValueError("未知规则: ...")。
- 和配置 `rules.enabled` 求交集；交集为空 → ValueError("请求的规则均未启用")。
- `force_send` 为真但 `send` 为假 → ValueError("强制发送只能与真实发送同时启用")。
- `max_send_orders` 非 None 时：`send` 必须为真；取值必须在 1–5。

## 主流程（顺序不能改）

1. `now = datetime.now(ZoneInfo(配置时区))`，生成 `run_id = uuid4().hex`。
2. **归档源文件**：如果文件不在 downloads 目录下，复制一份到
   `downloads/{YYYYMMDD}/manual-{run_id 前12位}{后缀}`；已经在 downloads 下就原地用。
   后续所有步骤都用归档后的路径（保证出问题时能复现）。
3. 算文件 SHA-256（分块读，1MB 一块），`begin_run(...)`。
4. `read_orders(...)`。
5. **行数护栏**（见下），不通过就抛异常。
6. `upsert_orders(...)`。
7. `rules.evaluate(...)`。
8. 打两组日志（见下）。
9. `sync_candidates(...)`。
10. 算 `send_scope`：`max_send_orders` 为 None 时就是全部候选；否则按**唯一订单数**截断
    （同一个订单命中的多条规则要一起保留或一起丢弃，不能只发一半）。
11. 算 `sendable`：`force_send` 时等于 `send_scope`（并打 WARNING
    "人工验收强制发送已启用，本轮忽略历史发送去重记录"）；否则逐条过 `should_send`。
12. 发送：仅当 `send and (sendable or force_send)`。
    - 发送前先 `config.validate(sending=True)`（校验 app_id / chat_id 已配）。
    - 调 `format_combined(selected_rules, sendable, current_candidates=send_scope)`，
      发一条消息，成功后 `mark_sent`，异常时 `mark_failed` 并把异常继续往上抛。
    - 注意 `force_send` 且 `sendable` 为空时**也要发**——这是人工验收场景，
      需要收到一条"四条规则当前都无命中"的汇总来确认链路通。
    - 不发送时，按规则码统计打一条演练日志；没有待发时打 "没有新的待发送提醒"。
13. `complete_run(status='success', ...)`，返回
    `RunResult(run_id, 归档路径, 行数, 候选数, 发送数, dry_run=not send)`。
14. **任何异常**都要先 `complete_run(status='failed', error=str(exc))` 再重新抛出。

## 行数护栏（必须在写库之前）

背景（请把这段写进 docstring）：TMS 的列表视图状态是账号级共享且粘性的——默认视图就是
"这个账号上一次操作过的视图"。人工在浏览器里把筛选切成一个只有 38 条的条件之后，
自动化用同一个账号登录会原样继承，于是导出了 38 条而不是 4750 条。致命之处在于当时
所有校验都通过了：页面总数 38、Excel 行数 38，两者一致，条数容差和 UI 比对查不出任何异常，
于是照常算规则、照常发飞书——R1 凭空冒出 36 个候选，R3 从 274 掉到 0。
所以要有一道**跟历史比**的合理性检查。

实现：
- 取 `recent_successful_row_counts(5)`，没有历史就跳过（首次运行）。
- 基线 = 这些行数的**中位数**（不是平均数）。用中位数是为了让单独一次"错误但成功"的运行
  不会把基线带偏，否则下一轮正常数据反而会被判成异常。
- 基线 < 100 时跳过：数据量本来就小的时候，比例判断纯属噪声。
- `min_row_ratio > 0` 且 `本轮行数 < 基线 × min_row_ratio` → 拒绝；
  `max_row_ratio > 0` 且 `本轮行数 > 基线 × max_row_ratio` → 拒绝。
  两个阈值都要有：只挡"变小"是不够的，实测出现过继承到 12644 行（正常 4750）的视图。
- 拒绝时的错误信息要写清楚发生了什么、怎么恢复：
  "本轮解析到 {X} 行，相对最近几次成功运行的中位数 {B} 行不足 {p}%（下限 {L} 行），
   疑似 TMS 视图被切换成了别的筛选条件，已拒绝处理以免基于错误数据发提醒。……"

## 两组必须有的日志

a) 候选统计：按选中的规则输出，R3 要拆成两个场景分别计数，例如
   `最新规则候选统计: R1=0, R3场景一=274, R3场景二=3, R4=0`
b) 规则前置条件统计：
   `规则前置条件统计: 订单总数=4750, 离厂时间为空=12, 有WMS过账时间=4700,
    是否延迟为是=0, 实际到达=签收时间=277`
   理由（写进注释）：R1/R4 经常每轮都是 0，光看候选统计分不清是"数据本来就没命中"
   还是"取数口径把这些单子过滤掉了"。把前置条件单独计数，0 候选时可以直接判断根因。
```

---

## 8. 配置项清单（给模型的配置提示词）

```text
用 YAML + dataclass 实现配置加载，`AppConfig.load(path)`，加载后立刻 `validate()`。
相对路径一律相对**配置文件所在目录**解析成绝对路径。
YAML 里出现的未知键**忽略**（只取 dataclass 里已定义的字段），不要报错。

部分字段允许被环境变量覆盖（环境变量优先，空串视为未设置）：
  RUNBOW007_TMS_USERNAME、RUNBOW007_FEISHU_APP_ID、RUNBOW007_FEISHU_CHAT_ID

本链路相关的配置项：

runtime:
  timezone: Asia/Shanghai      # 全流程时间基准
  downloads_dir / data_dir / logs_dir
  database_path: data/xxx.db
  retain_days: 30              # Excel 与日志保留天数

feishu:
  app_id: ""                   # App Secret 不进配置文件
  chat_id: "oc_xxx"            # 目标群
  mention_user_id: ""          # 空 = 不 @；填 open_id/user_id 才真正 @
  mention_name: "某某"
  request_timeout_seconds: 20

rules:
  enabled: [R1, R2, R3, R4]
  wms_lead_minutes: 90         # R1 阈值，校验范围 1–1440
  unresolved_repeat_hour: 9    # R3/R4 每日重复提醒的最早小时，0–23
  reopen_grace_hours: 12       # 命中消失多久后再出现才算新问题，0–720
  min_row_ratio: 0.5           # 行数护栏下限，[0,1)，0 表示关闭
  max_row_ratio: 1.5           # 行数护栏上限，必须 > 1，0 表示关闭

校验失败统一抛 `ConfigError(ValueError)`，信息里带上具体字段名和允许范围。
另外提供 `validate(sending=True)`：真实发送前额外要求 app_id 和 chat_id 非空，
报错文案 "真实发送前必须配置: feishu.app_id, feishu.chat_id"。
```

---

## 9. 测试提示词（用例清单）

```text
用 pytest 写测试，覆盖率门槛 75%。提供一个 `make_order` fixture 造 Order，
默认值是一条"完全正常"的订单，测试里只覆盖关心的字段。
写 .xls 测试文件用 xlwt（dev 依赖），.xlsx 用 openpyxl。

## 解析
1. .xlsx 和 .xls 各读一遍，字段值和类型一致（同一份数据两种格式，断言解析结果相等）。
2. .xls 的日期列读出来是 datetime，不是浮点数。
3. 表头用别名（"离厂时间" 而不是 "离厂时间(承运商提货时间)"）也能解析。
4. 缺必填表头 → WorkbookValidationError，信息里含缺失的中文表头名。
5. 订单号重复 → 报错且带行号。
6. 整行全空被跳过，不算数据行。
7. 行的列数少于表头数时不崩。
8. 是否延迟 = "是"/"否"/空 分别得到 True/False/False；填 "abc" 报错。
9. 延迟原因是 "   " 时归一成 None。
10. 页面总数与行数差值 <= 容差 → 只 warning；> 容差 → 报错。

## 规则
11. R1：过账 91 分钟且无离厂时间 → 命中；89 分钟 → 不命中；有离厂时间 → 不命中；
    无过账时间 → 不命中。
12. R1：now 带时区时不会因为时区比较报 TypeError。
13. R2：实际到达日期 = 今天且在途 → 命中；状态写成 "运输在途（已离厂）" 同样命中；
    昨天到达 → 不命中。
14. R3：两个场景各命中一次；实际到达 != 签收时间时两个场景都不命中。
15. R4：延迟且原因为空/空白 → 命中；有原因 → 不命中。
16. 只请求 R1 时，只产出 R1 候选。
17. 请求了配置里没启用的规则 → 该规则不跑。
18. 同一订单同时命中多条规则 → 产出多条候选，event_key 互不相同。

## 去重状态机
19. 首次命中 → should_send 为 True；mark_sent 之后同一轮再问 → False。
20. R1 发过之后第二天再问 → 仍然 False。
21. R3 发过之后，第二天 09:00 之后问 → True；第二天 08:00 问 → False。
22. 命中消失一轮（订单仍在报表里）→ 事件被置 resolved；
    在 reopen_grace_hours 内又出现 → last_sent_at **保留**，不会重复推送；
    超过 grace 后再出现 → last_sent_at 清空，可以重新推送。
23. 订单本身不在本轮报表里（跨月）→ 它的 open 事件不被误置 resolved。
24. mark_failed 之后 last_sent_at 仍为空，下一轮会重试。

## 消息
25. 配了 mention_user_id → content[0][0] == {"tag":"at","user_id":...}；没配 → 首行是纯文本。
26. 四条规则的明细文案逐字断言（对照附录 B）。
27. R3 只有一个场景有数据时，另一个场景的小标题不出现。
28. 汇总消息里，某规则本轮无新增但仍有命中 → 出现 "本轮无新增提醒（此前已提醒）"。
29. 某规则完全无命中 → 出现 "无符合条件订单。"
30. current_candidates 比 candidates 多时，出现 "另有 {K} 个此前已提醒"。
31. 四条规则全跑时标题是 "R1–R4订单提醒汇总"；跑 R1,R3,R4 时是 "R1、R3、R4订单提醒汇总"。

## 发送
32. 假 session 返回 code=0 → 返回 message_id；请求体里 content 是**字符串**且
    反序列化后结构正确；query 参数 receive_id_type=chat_id。
33. 返回 500 → 重试；第 3 次仍失败 → FeishuError。
34. 返回 400 → 不重试，直接 FeishuError。
35. token 在有效期内复用，不重复请求 token 接口。
36. 响应不是 JSON 时不崩，错误信息里带截断后的响应文本。

## 编排
37. send=False 时，全流程跑完、数据库有记录、但一次网络请求都没发。
38. 解析失败时 runs 表里那条记录 status='failed' 且 error 非空。
39. 行数只有历史中位数的 10% → 拒绝处理，且**订单没有写进 orders 表**。
40. 历史不足 / 基线 < 100 时护栏不生效。
41. max_send_orders=2 时，发送范围内的唯一订单数 <= 2，且同一订单的多条规则候选不会被拆散。
42. force_send 且当前无命中 → 仍然发出一条"全部无符合条件订单"的汇总。
43. 端到端：给一个含四类异常的 Excel，跑两轮，第二轮不重复发送。
```

---

## 附录 A：报表字段样例

一行真实数据（列名 → 值）：

| 中文表头 | 样例值 | 解析后类型 |
|---|---|---|
| 订单号 | `SO2026081900123` | str |
| 所属组织 | `华东大区` | str |
| 承运商名称 | `XX物流` | str |
| 离厂时间(承运商提货时间) | `2026/08/19 08:30:00` 或空 | datetime \| None |
| WMS过账时间 | `2026/08/19 06:15:00` | datetime \| None |
| 预计到达时间 | `2026/08/20 18:00:00` | datetime \| None |
| 状态 | `运输在途（已离厂）` / `已签收` | str |
| 合同状态 | `签署中` / `已完成` | str |
| 总箱数 | `36` | int（空 → 0） |
| 实际到达时间 | `2026/08/19 14:20:00` | datetime \| None |
| 签收时间 | `2026/08/19 14:20:00` | datetime \| None |
| 是否延迟 | `是` / `否` | bool |
| 延迟原因 | `天气原因` 或空 | str \| None |
| 承运商时效 | `48` | float \| None |
| 电子签签署时间 | `2026/08/19 16:00:00` | datetime \| None |
| 明细单总数 | `5` | int \| None |

---

## 附录 B：渲染后的消息成品（用来逐字对答案）

配了 `mention_user_id`、跑 R1/R3/R4 的一轮：

```text
标题：R1、R3、R4订单提醒汇总

@许昊 请关注以下订单：
【R1｜WMS过账时效预警】
共 2 单，离厂时间为空且WMS过账已超过配置阈值。
- SO2026081900123｜箱数 36｜WMS过账已 128.4 分钟，仍无离厂时间
- SO2026081900187｜箱数 12｜WMS过账已 95.0 分钟，仍无离厂时间
【R3｜合同签署状态异常提醒】
当前符合条件共 274 个订单；以下为本轮新增或到期重提醒的 3 个订单，另有 271 个此前已提醒。
【客户未电子签】
总共 2 个订单，总共 48 箱。
- SO2026081800045｜箱数 20
- SO2026081800061｜箱数 28
【运营未操作签收】
提醒内容：共 1 个订单。
- SO2026081700310
请运营人员将状态更新为「已签收」，合同状态为「已完成」。
【R4｜延迟无原因提醒】
无符合条件订单。
```

四条规则全跑、且 R2 有命中时的 R2 段：

```text
【R2｜今日签收提醒】
总共 5 个订单，总共 143 箱。
- SO2026081900201｜箱数 30
...
```

某规则本轮无新增但仍有存量：

```text
【R4｜延迟无原因提醒】
当前仍有 7 个符合条件订单；本轮无新增提醒（此前已提醒）。
```

---

## 附录 C：飞书 API 请求样例

取 token：

```http
POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal/
Content-Type: application/json

{"app_id": "cli_xxx", "app_secret": "xxx"}
```
```json
{"code": 0, "msg": "ok", "tenant_access_token": "t-xxx", "expire": 7200}
```

发消息：

```http
POST https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id
Authorization: Bearer t-xxx
Content-Type: application/json; charset=utf-8

{
  "receive_id": "oc_xxxxxxxxxxxxxxxx",
  "msg_type": "post",
  "content": "{\"zh_cn\":{\"title\":\"R1、R3、R4订单提醒汇总\",\"content\":[[{\"tag\":\"at\",\"user_id\":\"ou_xxx\"},{\"tag\":\"text\",\"text\":\" 请关注以下订单：\"}],[{\"tag\":\"text\",\"text\":\"【R1｜WMS过账时效预警】\"}]]}}"
}
```
```json
{"code": 0, "msg": "success", "data": {"message_id": "om_xxx"}}
```

官方文档：
- 发消息 https://open.feishu.cn/document/server-docs/im-v1/message/create
- 鉴权 https://open.feishu.cn/document/server-docs/api-call-guide/calling-process/get-access-token

前置条件（写代码之前先让人确认）：自建应用已开启机器人能力、已发布、已被拉进目标群、
有发消息权限；`mention_user_id` 必须是该租户下真实的 open_id/user_id，乱填会导致整条消息发送失败。

---

## 附录 D：已经踩过的坑（"不要这么做"清单）

给模型的负面约束，每一条都对应过一次线上问题：

1. **不要在规则层查数据库。** 规则一旦依赖历史，就没法用固定输入测试，
   线上出问题也无法复现。"发不发"是数据库层的事。
2. **不要用列序号定位字段。** 导出模板的列顺序变过，必须按中文表头名 + 别名匹配。
3. **不要相信"页面总数 == Excel 行数"就说明数据对。** 视图被切成别的筛选时，
   两个数是一致的，只是都错了。必须再跟历史行数比一次。
4. **不要一发现命中消失就清空发送记录。** 数据每小时都在抖，同一单会反复消失/出现，
   清早了就会重复刷屏。要有 reopen 静默期。
5. **不要把 resolve 写成"本轮没命中就置 resolved"。** 必须加
   `order_no IN (本轮报表里的订单)`，否则跨月订单会被误判成已解决，下个月又重新提醒一遍。
6. **不要给 Excel 时间贴时区。** 报表里是裸的北京时间，贴时区再和 `now` 比会差 8 小时。
   正确做法是把 `now` 降成裸时间。
7. **不要把飞书的 content 当对象传。** 必须是 `json.dumps` 之后的字符串。
8. **不要把 app_secret 写进 YAML 或异常信息。** 从凭据库/环境变量注入。
9. **不要在没有显式发送开关时发消息。** 默认演练，日志照打、库照写、就是不发。
10. **不要"优化"文案。** 群里的人和后续的自动化都在按固定句式识别消息。
11. **不要把 xlrd 换成别的库来读 .xls。** xlrd 2.x 不支持 .xlsx 是设计如此，两种格式分开处理。
12. **不要在失败时静默吞异常。** runs 表要落 `status='failed'` 和 error，
    否则监控上看不出"这一轮根本没跑成"。
