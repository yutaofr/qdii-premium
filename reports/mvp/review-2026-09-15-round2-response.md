# MVP 再审处理记录

日期：2026-09-15。依据：[review-2026-09-15-round2.md](review-2026-09-15-round2.md)（检查 HEAD `d427656`）。

## 结论

F1—F5 全部在修复前复现并确认，均按再审的修复要求修正，每项都有回归测试（F1 用两条相反的用例）。上一轮回应中两处表述超出了实现，本次撤回：

- "R1—R6 已全部关闭、G6 全部通过"
- "覆盖文件严格校验、任一非法整份不加载"（当时只校验了顶层键）

下表的关闭状态是本方的**建议**，需经再审确认。产品定位不变：交付仍是**相对比较研究预览**，不能判断绝对溢价是否满足买入条件。美股收盘期货锚点模块（`d427656`）不在本次修复范围内，尚未经评审，也不作为本次关闭依据。

## 修复前复现（与再审一致）

| 项 | 复现结果 |
|---|---|
| F1 | 513100 的 09-14 净值被绑定到 09-11 指数（29368.44），五只全部准入，common_anchor=False |
| F2 | 竞价 opening 只写一个时间时抛 IndexError；closure 中嵌套未知键 `clsoed` 时 errors=[] |
| F3 | 先收到未来时间的行情，之后的正常行情被当作旧行情拒绝，价格停在 9.999 |
| F4 | 页面 bundle_id `f579b5278e19e492`，下载得到 `41ade88640ae7191` |
| F5 | 一只基金净值为 08-14 时，全组锚点交易日数为 22；其余四只的全部成对判断都降为 MODEL_REFERENCE |

## 逐项处理

### F1 · 跨日归一化使用错误的指数日期

**原因：** 取的是"已收到、且不晚于净值日的最大日期"。这把美股真实休市和数据未到混成了同一种回退。

**修正（ADR-030）：** `RelativeState._anchor_factors`（`src/qdii/pipeline/relative_state.py:227`）改为三步：

1. 用 `CalendarProvider.last_session_on_or_before("NASDAQ", 净值日)` 求出应有的指数日 a。沿途任一天不在日历覆盖范围内就返回空，不按工作日推断。
2. 精确取 a 日的收盘。a 是交易日但收盘未到时，该成员不提供跨日因子，随后按最大同日期子集规则处理。
3. 中间价仍精确取净值日当日。

输入包新增 `index_date` 和 `fx_date`；缺失原因写入 notes，例如 `513100:index_close_missing:2026-09-14`。另外，有因子的成员恰好同一天时，改按共同锚点比较（因子相消，结果不变），`common_anchor` 标记与实际一致。

**测试：**

- `test_f1_missing_close_on_us_trading_day_is_not_replaced_by_older_close`：09-14 是美股交易日但收盘缺失。513100 不得绑定 09-11 的值，退出比较；其余四只按 09-11 共同锚点继续。
- `test_f1_real_us_holiday_uses_previous_session_close`：净值日 2026-09-07 是美国劳工节，A 股正常交易。指数日回退到 09-04，五只按完整公式比较。
- `test_last_session_on_or_before_uses_calendar_not_weekdays`

### F2 · 覆盖文件结构校验不完整

**修正（ADR-031）：** `_parse_overrides`（`src/qdii/io/calendars.py:114`）现在完成全部结构和语义校验，并返回已校验的 `Overrides` 对象。构造器只使用这个对象，不再读取原始 TOML 结构。校验内容：

| 部分 | 校验 |
|---|---|
| alias | 必须是表，每项只含 `calendar`，且是库中已知的日历 |
| closure | 必须是表数组。字段集合为 market/date/source 必填，received_at/reason 可选；逐字段检查类型；日期必须是 ISO 字符串；市场必须已知 |
| auction | 市场必须已知，字段集合为 tz/opening 必填、closing 可选；时区可解析；区间恰好两个 "HH:MM" 字符串，且开始早于结束 |

任一处非法都统一转为 `CalendarOverrideError`：整份不加载，所有市场视为未覆盖。

**测试：** `test_f2_invalid_override_structure_rejected_without_exception` 覆盖 12 种非法结构，包括再审的三例 `alias = 1`、opening 只写一个时间、嵌套 `clsoed`。每种都断言：

- 构造不抛出，errors 非空，版本带 UNCERTAIN；
- `phase` 为 UNKNOWN、覆盖为 False；
- NASDAQ 未覆盖，`last_session_on_or_before` 为空，即跨日因子同样不可用。

另有反例 `test_f2_full_closure_record_with_optional_fields_accepted`：带可选字段的合法记录正常生效。

### F3 · 未来行情占住最新指针

**修正（ADR-032）：** `RelativeState.ingest`（`relative_state.py:153`）先检查供应商时间与接收时间：供应商时间领先接收时间超过 2 秒（与 `core.relative` 的 FUTURE_TIMESTAMP 容差一致）时，该行情不写入 latest/reference，计入 `future_rejected`。原始报文照常留在原始日志中供审计。

**测试：** `test_f3_future_timestamp_does_not_block_recovery`。先注入 provider 14:59:55、received 14:49:59 的异常行情，再注入正常行情。14:50:02 的输入包价格为正常值，五只全部准入，`future_rejected=5`。

### F4 · 下载的不是页面展示的输入包

**修正（ADR-033）：**

- 采集器每次生成状态页时，把完整输入包按 bundle_id 放入内存有界缓存（`BundleCache`，64 个，`src/qdii/apps/status.py:31`）。
- 页面链接改为 `/relative/bundle/<bundle_id>.json`，下载时只从缓存取回，不重新构包。
- id 未知或已被淘汰时返回 404，并提示刷新页面或用 `qdii relative --save`。
- 无 id 的旧路由 `/relative/bundle.json` 已删除。
- 页面注明：`--save` 保存的是新的决策快照，不能替代页面上的快照。

**测试：** `test_f4_downloaded_bundle_is_the_one_shown_on_the_page`，端到端。真实 `Collector._status_snapshot` 加真实 HTTP 服务，时钟每次读取前进 1 秒；取页面后再刷新一次，然后按页面链接下载。断言：

- 下载内容的 bundle_id 等于页面上的 id，且等于包内容重新哈希的结果；
- 未知 id 和旧路由都返回 404。

**实机验证：** 采集器已用新代码重启。取页面、再刷新一次、再按页面链接下载，得到同一 bundle_id（`05500b2d062cb81f`，schema 3）；旧路由返回 404。

### F5 · 被剔除成员的旧锚点影响其余成员

**修正（ADR-034）：**

- **状态层**（`relative_state.py`）按成员计算 `anchor_sessions`（净值日之后已完成的 A 股交易日数）和 `anchor_calendar_covered`。输入包的 `calendar_covered` 只表示截止日。成员锚点日期不在覆盖范围内时，只给该成员加 CALENDAR_UNCERTAIN。
- **边界**只存单日 P95，放大在纯函数中按成员对完成：`√max(1, k_i, k_j)`（`src/qdii/core/relative.py:269`）。任一成员锚点为 EXTENDED 或 INVALID 时，仅该成员对降为 MODEL_REFERENCE（原因分别为 ANCHOR_EXTENDED / ANCHOR_INVALID）。
- **组级锚点健康**与 `max_anchor_sessions` 只取参与比较的成员（`src/qdii/core/relative_bundle.py:172`）。
- **输入包升级为 schema 3。** 回放遇到旧结构快照时报 VERSION_CHANGED，不崩溃也不计为通过（`test_old_schema_snapshot_reported_as_version_change_not_crash`）。

**测试：**

- `test_f5_excluded_member_anchor_does_not_degrade_others`，参数为 08-14（过旧）和 1999-01-04（超出日历覆盖）。被剔除成员不影响其余四只：锚点 NORMAL、最大交易日数 2、无 ANCHOR_EXTENDED 或 CALENDAR_UNCERTAIN，全部成对判断都有边界。
- `test_f5_pair_bound_scales_with_its_own_members_anchor_age`：边界按成员对放大；EXTENDED 或锚点未知只影响涉及该成员的成员对。

## 验证

- `pytest` 165 项全部通过；在 `TZ=Pacific/Auckland` 和 `TZ=America/Los_Angeles` 下同样通过。`ruff` 无问题；分层依赖契约通过。
- 用生产原始日志按 schema 3 重新生成 2026-09-15 回放证据：1290 个触发点，输入包重算和流回放均 PASSED（1290/1290），摘要见 [evidence/replay-2026-09-15.json](evidence/replay-2026-09-15.json)。与上一轮一样，这是**重新生成的样本**，不是生产当时写入的快照。
- 实机输入包的 notes 显示五只都是 `index_close_missing:2026-09-11`。原因是 Nasdaq 与 CCPR 端点在上一轮修正后还没经过 A 股窗口，尚无数据。日期一致时不需要跨日因子，当前结果不受影响；按 F1 规则，因子到齐前不会用旧值替代。
- 修正过程中发现证据工具的清理步骤在输出目录已存在时会失败（`mkdir`）。现改为 `shutil.rmtree`，它不跟随其中指向生产原始日志的符号链接；已核对生产原始日志文件数不变（29 个）。

## 逐项关闭状态（建议，待再审确认）

| 原问题 | 状态 |
|---|---|
| R1 差分边界 | 研究预览可接受；盘中适用性仍未验证（G7 有条件），不能升级为可信的实时经济判断 |
| R2 同值后补事件 | 关闭（再审已确认） |
| R3 迟到旧报价 | 原缺陷关闭；恢复路径 F3 已修正，建议关闭 |
| R4 异日全退出 | 原场景关闭；F1 与 F5 已修正，建议关闭。跨日因子的生产数据从 2026-09-16 窗口开始积累，届时核验首个跨日样本 |
| R5 空回放/证据 | NO_DATA 与重新生成的证据已补；**生产写盘仍待 2026-09-16 窗口核验**，不关闭 |
| R6 日期越界 | 构包越界场景关闭；F2 已修正，AT58 建议判通过 |
| 官方净值价格口径 | 关闭（再审已确认） |
| 完整包与决策留存 | F4 已修正，端到端测试和实机都确认"导出 bundle_id = 页面 bundle_id"，建议关闭 |

## 仍未完成（不因本次修正改变）

- 生产快照写盘核验（2026-09-16 窗口）。
- G1 官方净值抽查：每只基金 3 个日期。
- G7 盘中边界校准。
- G0 产品路径决定。
- E 路径：E-NAV 与绝对估算溢价未实现；收盘期货锚点模块尚未经评审。
