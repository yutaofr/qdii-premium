# MVP 三审处理记录

日期：2026-09-15。依据：[review-2026-09-15-round3.md](review-2026-09-15-round3.md)（基线 `16b4b1c`）。

## 结论

接受三审判定：F1、F3、F4、F5 关闭；F2 部分关闭。C1、C2 在修复前均已复现并确认。

覆盖文件这一块连续两轮出现逐例漏洞，这次不再逐例补丁，改为两处结构性修正：

1. 日历的所有公开查询统一经由一个解析入口，先判断不确定状态，再解析日历。
2. 用模糊测试把"不抛出、拒绝后全面降级"固定为不变式。

修正过程中另外发现两处同类缺口（C3、C4），一并处理。本次仍只是修复核验，不是发布通过，也不涉及绝对溢价判断能力。

## 修复前复现

| 项 | 输入 | 结果 |
|---|---|---|
| C1 | `opening = ["09:15+01:00", "09:25"]` | 构造时抛 `TypeError: can't compare offset-naive and offset-aware times` |
| C2 | `alias = 1` | SSE、NASDAQ、XSHG 能返回 UNKNOWN；SZSE 的 `phase` 和 `sessions_between` 都抛 `InvalidCalendarName` |
| C3（本方发现） | `tz = "America"` | `ZoneInfo` 抛 `IsADirectoryError`（属于 OSError），原校验没有捕获 |
| C4（本方发现） | 台账文件不是合法 JSON，或结构、日历名非法 | 构造时抛异常，没有降级 |

SSE 和 NASDAQ 能正常降级，是因为 exchange_calendars 自带这两个别名，不是因为代码有保护。

## 修正

### C1、C3 · 时间与时区的严格校验

- 竞价时间改为严格正则 `HH:MM`（00:00—23:59），直接构造不带时区的 `time`，不再使用 `time.fromisoformat`（`src/qdii/io/calendars.py:106`）。偏移、秒、单位数小时、24:00 都会被拒绝。
- 开始必须早于结束，比较只发生在两个已校验的时间之间，不会再出现类型错误。
- 时区解析失败统一转为覆盖错误，捕获范围增加 OSError（`calendars.py:179`）。

### C2 · 统一的日历解析入口

新增 `CalendarProvider._resolve(market)`（`calendars.py:255`），作为 `covers`、`session`、`sessions_between`、`phase`、`last_session_on_or_before` 的唯一入口。依次处理：

1. 覆盖文件整份拒绝时，直接返回空，不解析日历。
2. 该日历回退被拒时，返回空。
3. 市场名无法解析时，返回空，并在 `errors` 中记录一次（出现过一次的错误不重复记录）。

调用方拿到空值一律按未覆盖处理：阶段 UNKNOWN、会话为空、交易日列表为空。`sessions_between` 也改为先检查覆盖，再查询区间。

### C4 · 台账不可读

- 台账读取与结构校验集中到 `_read_ledger`（`calendars.py:187`）。要求是映射，键为库中的规范日历名（台账只写解析后的名字），值为 ISO 日期字符串列表。
- 不满足时记录 `closure ledger unreadable`，所有市场不确定，**且不改写台账**。
- 原因：台账无法读取时，就无法判断新版覆盖文件是否删除了已生效的休市日，只能整体不确定。

## 测试

- **非法结构用例：** `test_f2_invalid_override_structure_rejected_without_exception` 从 12 种扩到 17 种，新增带偏移、带秒、单位数小时、24:00、`tz = "America"`。
- **断言范围：** 改为共用的 `assert_degrades_everywhere`，对 SSE、SZSE、NASDAQ、XSHG 逐一验证 5 个查询都降级，不再只看 `covers` 或 `last_session_on_or_before`（按三审建议）。
- **未知市场：** `test_c2_unknown_market_with_valid_overrides_degrades_and_is_recorded`。覆盖文件合法时查询未知市场，结果降级并记录错误；SZSE 等其他市场不受影响。
- **台账：** `test_unreadable_ledger_makes_all_markets_uncertain_and_is_not_rewritten`，6 种坏台账。
- **模糊测试：** `tests/property/test_calendar_overrides_props.py`，250 个样例。在合法覆盖文件上随机替换值、改键名、插入键、改表头、删行，可替换的值包含上述所有已知陷阱。不变式有两条：
  - 构造和 5 个查询在 5 个市场（含未知市场）上都不抛出；
  - 版本带 UNCERTAIN 时，所有市场的所有查询都降级。
- **模糊测试的有效性：** 临时换回修复前的 `calendars.py` 运行该测试，第一个失败样例就是删掉 `calendar = "XSHG"` 那一行导致 SZSE 抛 `InvalidCalendarName`，即 C2。换回修复后的代码则通过。

## 验证

- `pytest` 178 项全部通过；在 `TZ=Pacific/Auckland` 和 `TZ=America/Los_Angeles` 下也都退出码为 0。`ruff` 无问题；分层依赖契约通过。
- 采集器已用新代码重启（run `LIVE-20260915T113927Z-home-mac`）。事件日志中没有 CALENDAR_UNCERTAIN，说明生产覆盖文件合法；状态页相对比较正常计算。
- 本轮没有改动输入包、状态层或回放代码，schema 仍为 3，上一轮的回放证据仍然适用。

## 关闭状态（建议，待确认）

| 项目 | 状态 |
|---|---|
| F1、F3、F4、F5 | 关闭（三审已判定） |
| F2 覆盖文件校验 | C1—C4 已修正，并有非法结构用例和模糊测试不变式；建议关闭 |
| AT58 | 建议判通过：非法结构、未知市场、不可读台账都整份拒绝，所有市场的所有查询都结构化降级 |

## 仍未完成（与三审一致，不因本次修正改变）

1. 生产窗口快照实际写盘与回放核验（2026-09-16 窗口）。重新生成的 1290 点不能替代。
2. G1：每只基金 3 个日期的官方净值抽查。
3. G7：盘中边界适用性验证。
4. G0：能力矩阵及明确的产品路径决定。
5. E-NAV 与买入所需的绝对估算溢价未实现；美股收盘锚点模块尚未经完整评审。
