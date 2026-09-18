# qdii-premium

纳指100 QDII ETF 溢价监测。当前状态：研究预览。A 股交易时段提供**盘中代理估算溢价**（纳指期货实时价相对昨结算的涨跌 + 即期汇率推算估算净值；结算日未验证、无误差范围，见勘误 E8；亚洲决策时点误差未验证，不能据此确认是否满足买入条件），收盘后显示收盘参考估算；另有五只之间的**相对比较**。打开或唤醒电脑即可使用，不需要夜间开机。

- MVP 验收：[reports/mvp/acceptance.md](reports/mvp/acceptance.md)；评审与处理：[review](reports/mvp/review-2026-09-15.md)、[response](reports/mvp/review-2026-09-15-response.md)

- 需求与规范：[docs/spec/](docs/spec/)（SRD 1.3、DS/VM/QS 1.0）及 [勘误](docs/SRD_v1.3_errata.md)
- 架构：[docs/ADD-0_Phase0_and_Core.md](docs/ADD-0_Phase0_and_Core.md)
- Phase 0 证据：[reports/phase0/](reports/phase0/)

## 模块

| 模块 | 内容 |
|---|---|
| `qdii.core.types` | 时间、RawMessage、MarketQuote/QuoteSide、QS-01 枚举、QS-07 理由码 |
| `qdii.core.quote_norm` | QS-04 盘口一侧归一化（零值/非有限/缺失不进公式） |
| `qdii.contracts.sina_a_share_v1` | 新浪 A 股批量行情解析（带 Referer；供应商时间不当事件时间） |
| `qdii.io.rawlog` | 原始日志：按来源/UTC 小时分段、轮转压缩、崩溃尾行修复、有序回放读取 |
| `qdii.io.http` | 采集请求、跳转链与响应头记录、5→15→30→60 秒退避、403/429 持久封禁 |
| `qdii.apps.collect` | 采集守护进程：13 个端点；只在主机窗口（巴黎 07:30 至 A 股收盘）内请求并防睡眠；窗口内/外缺口分类记录 |
| `qdii.apps.status` | 只读状态页：仅本机 `http://127.0.0.1:8787`（防火墙保持阻止所有传入连接） |

| `qdii.contracts.eastmoney_lsjz_v1` / `index_history_v1` / `chinamoney_ccpr_his_v1` | 历史净值、NDX 收盘（Nasdaq / FRED）、USD/CNY 中间价 |
| `qdii.core.model_m0` / `history_validation` | VM-01 M0 纯函数；PH0-07 日期对齐 × 汇率规则对照（开发/封存切分） |
| `qdii.apps.research_nav` | `qdii research fetch-history` / `nav-fx`：抓取写原始日志，再从原始日志重解析出报告 |

| `qdii.io.calendars` | exchange_calendars（XSHG/XNYS）+ 覆盖文件；市场阶段含集合竞价与午休；未覆盖日期返回 UNKNOWN |
| `qdii.core.relative` | VM-10 相对比较：S、排名、成对 δ 与差分边界判断；共同锚点时不需要指数与汇率 |
| `qdii.apps.relative_snapshot` | `qdii relative --at …`：按知识截止时刻在原始日志上回放；连续交易用卖一价，其余阶段为收盘参考（勘误 E1） |

| `qdii.core.relative_bundle` | 不可变输入包（规范 JSON + SHA-256）、`evaluate_bundle`、质量对象（QS-01~03）、确定性比较 |
| `qdii.pipeline.relative_state` | 在线与回放共用的增量状态：as-of、净值修订隔离、因子组、收盘参考快照 |
| `qdii.io.snapshot_store` | 相对比较快照只追加 JSONL（ADR-018） |
| `qdii.apps.replay_relative` | `qdii replay relative`：输入包重算 / 流回放逐包比对 |
| `qdii.contracts.sina_hf_v1`、`qdii.core.anchor`、`qdii.pipeline.anchor_capture`、`qdii.io.anchor_store` | 美股收盘期货锚点：hf_NQ 解析、VM-03 选取、到期评估、只追加锚点库（ADD-0 §17） |
| `qdii.pipeline.host_windows` | A 股窗口 + 按日历计算的美股收盘窗口组合（收盘窗口已停用） |
| `qdii.core.enav`、`qdii.contracts.sina_hf_v2`、`qdii.contracts.cfets_fx_spot_v1` | 盘中估算净值与估算溢价（ADR-036）：昨结算代理锚点、CFETS 即期汇率 |

未实现：估算溢价的误差范围（亚洲决策时点需独立参照，见 [验证协议](reports/phase0/futures/asian-decision-validation-protocol.md)）、价格机会通知、基金公告监控、SQLite 规范化存储。

## 使用

- 首屏（本机）：`http://127.0.0.1:8787/`，数据接口 `/relative.json`（视图）、`/relative/bundle/<bundle_id>.json`（页面所示的完整输入包，按 id 取回）、`/health.json`
- 决策披露（2026-09-18 期货基差复审）：`estimate_available`（有当前代理估算）与 `absolute_decision_eligible`（通过绝对决策验证）是两个独立字段。亚洲决策时点的期货代理误差尚未验证，后者当前恒为 `false`，状态 `UNVALIDATED_ERROR`，`absolute_error_bound_bp` 为 `null`；视图、命令行、下载信封（`decision_disclosure`）一致。`absolute_premium_available` 是已弃用的兼容别名，只表示存在代理估算
- 命令行：`uv run qdii relative`（当前）；`uv run qdii relative --at 2026-09-15T14:50:00+08:00`（任意历史时刻）

## Phase 0 证据

| 项 | 结论 | 报告 |
|---|---|---|
| PH0-08 可达性 | R 核心端点从法国可达；新浪需 Referer；东方财富行情走延迟通道 | [reach/findings](reports/phase0/reach/20260914T231217Z/findings.md) |
| PH0-07 历史验证 | 五只基金均与"净值日美股收盘 + 当日中间价"一致，封存 MAE 0.5—0.7 bp | [history/findings](reports/phase0/history/RESEARCH-20260915T072333Z/findings.md) |
| PH0-06 基金文件 | 五只招募说明书估值汇率条款均为"估值日（当日）人民银行中间价"，与 PH0-07 一致 → 规则 VERIFIED；513390 有备选汇率来源条款 | [fund_rules/findings](reports/phase0/fund_rules/20260915T084057Z/findings.md)、`data/fund_rules/` |
| PH0-08 日历 | XSHG/XNYS 与 412 个净值日、425 个 NDX 收盘日完全一致；无 XSHE（映射 XSHG）；XSHG 仅覆盖到 2026-12-31 | [calendar/findings](reports/phase0/calendar/findings.md) |
| 事件 | 2026-09-15 合盖睡眠；采集范围改为巴黎 07:30 至 A 股收盘 | [incidents](reports/phase0/incidents/2026-09-15-clamshell-sleep.md) |

## 运行

```bash
uv sync
uv run pytest && uv run lint-imports && uv run ruff check src tests
```

```bash
uv run qdii collect --config-dir config            # 前台运行，数据写入 ~/qdii-data
./deploy/macos/install_agent.sh                    # 以 LaunchAgent 常驻
```

```bash
uv run qdii raw stats --date 2026-09-15                                   # 各端点录制统计（UTC 日期）
uv run qdii raw parse --contract sina_a_share_v1 --endpoint sina.etf_batch --date 2026-09-15
uv run qdii events --tail 30                                              # 启动/停止/缺口/封禁等事件
uv run qdii unblock sina.etf_batch                                        # 确认后解除 403/429 封禁
uv run qdii relative                                                      # 当前相对比较（非交易时段自动给收盘参考）
uv run qdii relative --at 2026-09-15T14:50:00+08:00 --json                # 回放任意历史时刻
uv run qdii replay relative --date 2026-09-15 --stream                     # 确定性回放校验（NO_DATA 退出码 3）
uv run qdii relative --save                                               # 保存当前决策快照
uv run qdii anchors                                                       # 最近的美股收盘期货锚点
uv run qdii replay anchors --date 2026-09-15                              # 从原始日志重算锚点并比对（美东日期）
uv run python tools/mvp_replay_evidence.py 2026-09-15                     # 重新生成回放证据与逐点延迟
uv run qdii research fetch-history --since 2025-01-01                     # 历史数据 → ~/qdii-data/research
uv run qdii research nav-fx                                               # 从原始日志重解析并生成 PH0-07 报告
uv run --group research qdii research fund-rules                          # PH0-06：招募说明书下载到仓库外并提取条款
```

数据目录 `~/qdii-data`：`raw/`（原始日志）、`anchors/futures/`（期货锚点）、`snapshots/relative/`（相对比较快照）、`events/collector.jsonl`、`state/heartbeat.json`、`state/blocked.json`、`logs/`。
