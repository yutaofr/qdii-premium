# MVP 验收报告：相对估值参考首版（R 路径）

日期：2026-09-15。范围：SRD 1.3 §5 的 11 项 P0 功能需求、R 路径适用的验收用例、§6 非功能需求，以及 §8 发布门槛。
结论：**MVP 软件已实现并在主机上运行**（LaunchAgent 常驻，采集窗口为巴黎 07:30 至 A 股收盘）。发布门槛 G0 仍依赖 Phase 0 的真实会话数（目标 10 个，当前 1 个），这一项需要时间积累，软件本身不再阻塞。

测试：`uv run pytest` 共 110 项全部通过（随机 `TZ` 下同样通过）；`lint-imports` 分层契约通过；`ruff` 无问题。

## 1 P0 功能需求

| FR | 要求 | 实现 | 验证 |
|---|---|---|---|
| FR01 | 五只基金为初始配置，以交易所 + 代码识别；因子组绑定 | `config/funds.toml`、`pipeline/relative_state.py::Fund`；不同因子组的基金退出比较（FACTOR_GROUP_MISMATCH） | `test_fund_rules.py`、`test_at69_other_factor_group_not_ranked` |
| FR02 | 首屏显示相对参考与独立质量；历史净值对照单列 | 状态页首屏 `apps/status.py::render_relative`：排名、价格与卖一量、相对最便宜、与下一名的差距及判断、官方净值溢价或折价、净值日、新鲜度 | `test_status_page_renders_first_screen`；实机 `http://127.0.0.1:8787/` |
| FR03 | 可展开查看计算输入、时间、来源、策略版本及退出原因 | 每只基金的"详情"（净值、快照时间、年龄、原因、消息 ID）；"输入包与版本"；`/relative.json`；`qdii relative --json` | 同上 |
| FR06 | 冻结输入包，按 as-of 与策略对齐；共同 τ | `core/relative_bundle.py`（规范 JSON + SHA-256）、`RelativeState.bundle`：只用 received_at ≤ 截止时刻的数据；乱序报文不倒退当前指针 | `test_relative_snapshot_replay.py`（AT35—37 语义）、`test_out_of_order_snapshot_does_not_rewind` |
| FR07 | 本地日历加人工覆盖，版本与时区记录，缺口隔离 | `io/calendars.py` + `config/calendar_overrides.toml`；版本进入输入包；日历未覆盖的日期返回 UNKNOWN | `test_calendars.py`（AT19/20/56）；PH0-08 与 412 个净值日、425 个 NDX 收盘日比对一致 |
| FR09 | 净值修订只追加；三态；事件不重复计算；无法支持时隔离 | `RelativeState._nav_for`：同一净值日数值不一致判 PENDING_VERIFY，有事件字段判 CORPORATE_ACTION_PENDING，增长率断点判待复核；原始日志只追加 | `test_at46_nav_revision_isolates_fund_and_duplicates_do_not`（AT46/49/50） |
| FR10 | 单基金故障不阻塞其他基金；共用 NQ 故障不阻塞 R | 成员级退出；R 不读取 NQ | `test_at46…`（其余四只继续）、`test_at62…`、`test_staleness_span_and_phase` |
| FR11 | 录制、不可变输入包与离线确定性回放 | 原始日志、`io/snapshot_store.py`、`qdii replay relative [--stream]` | `test_collector_mvp_integration.py`（真实 Collector → 快照 → 两种回放均零差异）；**真实数据：2026-09-15 共 1290 个触发点，两种回放均 0 差异** |
| FR12 | 按指标分别显示时效、来源可信度、模型状态、年龄、对齐及理由 | `RelativeQuality`：freshness、provenance_confidence、delay_status、model_status、anchor_health、alignment、max_skew、reason_codes，以及每只基金的年龄与新鲜度 | `test_at65_freshness_boundaries`、`test_at67_anchor_health`、`test_extended_anchor_never_robust_and_aged_is_flagged` |
| FR13 | 买入比较只用有效卖一；缺一侧时不补价 | `RelativeState` 只接受 VALID 卖一；缺失判 QUOTE_MISSING | `test_sina_a_share_v1.py::test_zero_ask_does_not_erase_valid_bid`、AT51—53 |
| FR26 | 价格与数量必须有限且为正；保留原文；区分无挂单、缺失、错误 | `core/quote_norm.py`、`contracts/sina_a_share_v1.py` | `test_quote_norm.py`、`test_quote_norm_props.py`（hypothesis 随机输入） |

## 2 R 路径的其他关键验收用例

| AT | 结果 | 依据 |
|---|---|---|
| AT01 | 通过 | 五只基金映射与规则证据一致（`test_fund_rules.py`） |
| AT06/07 | 通过（历史数据） | PH0-07：美股休市日 75 个区间、中国长假 30 个区间，残差均小于 1 bp |
| AT13/14 | 暂不适用 | R 首版遇到事件时让该基金退出比较，不做事件换算 |
| AT15 | 通过 | 累计净值不用作分母（解析器区分单位净值与累计净值） |
| AT27/50/70 | 通过 | as-of 截止与回放测试 |
| AT29 | 通过 | 新浪供应商时间标注为 PROVIDER_SNAPSHOT_UNVERIFIED，不当作成交时间 |
| AT40—43 | 部分通过 | 单边或缺失盘口不参与买入比较；精细的涨跌停封板标签属 P1（FR23） |
| AT55—60 | 部分通过 | 离线本地日历、覆盖缺口、时区版本已实现；覆盖文件的冲突校验与拒绝回退尚未实现（首版以停机重载代替） |
| AT61—63 | 通过 | `test_relative.py` |
| AT65/67 | 通过 | 质量对象测试 |
| AT69 | 通过 | 因子组隔离 |
| AT75 | 通过 | 窗口内与窗口外缺口分类记录，状态页可见 |
| AT77 | 通过 | 403/429 持久封禁，并显示 SOURCE_ACCESS_BLOCKED |
| AT78 | 通过 | 列表按 S 排序，成对判断单独展示，不生成唯一冠军 |

## 3 非功能需求

| NFR | 结果 |
|---|---|
| NFR01 计算延迟 P95 ≤1 秒 | 真实数据中，1290 个快照的解析、构包、计算与写盘合计 2.0 秒，约 1.5 毫秒/个 |
| NFR02 确定性 | 输入包重算与流回放均 0 差异（浮点容差 1e-10） |
| NFR04 故障不崩溃 | 单端点失败只退避；任务意外结束会记录并交给 launchd 重启；状态页计算失败不影响采集 |
| NFR06 版本 | 输入包记录日历、时区库、契约、差分边界、净值汇率规则与因子组的版本 |
| NFR07/08 | 不需要任何凭据；状态页只在本机开放；不执行供应商返回的脚本 |
| NFR09 手机可读 | 页面已做响应式布局；但按维护者的防火墙决定，状态页只能在本机访问 |
| NFR10 状态页 | 已实现 |

## 4 发布门槛（R）

| Gate | 状态 | 说明 |
|---|---|---|
| G0 Phase 0 决定 | **进行中** | 有效会话 1/10（2026-09-15 窗口内完整录制）；国庆前还有约 9 个交易日 |
| G1 证券与净值 | 部分 | 身份、单位、日期正确；官方净值只抽查了 1 个点，每只基金需抽查 3 个日期 |
| G2 规则与相对准入 | 通过 | 五只基金的净值汇率规则均为 VERIFIED（PH0-06 + PH0-07） |
| G5 时效 | 通过（R） | QS-02 的 R 策略已实现；R 不依赖 NQ |
| G6 适用用例 | 通过 | 见第 1、2 节；少量 P1 与部分项已如实列出 |
| G7 标签 | 通过 | ROBUST_DIFFERENCE 仅在"日终历史差分 P95 × √交易日数"的边界之内授予，并标明来源；EXTENDED 锚点不授予 |
| G8 故障与回放 | 通过 | 一个真实会话加合成故障（零盘口、乱序、缺净值、修订、日历缺口）均可回放 |
| G9 机会通知 | 不适用 | 首版关闭 |
| G10 版本与资源 | 通过 | 本地日历与规则版本齐全；运行资源为主机可用窗口（ADD-0 §15） |

## 5 已知限制

- 冬令时（10-25 起）采集窗口只有 35 分钟；绝对估值（E 路径）不在 MVP 范围内。
- 状态页只能在本机访问（维护者决定）。
- 新浪供应商时间尚未经盘中验证；东方财富行情走延迟通道，仅作对照。
- 基金公告监控（汇率规则变更、分红）未实现：事件会通过净值字段触发隔离，但公告本身不会被提前感知。
