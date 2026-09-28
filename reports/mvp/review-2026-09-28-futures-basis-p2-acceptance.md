# 期货基差四项 P2 修复复验：886ac86

日期：2026-09-28。范围：`0cb2b06..886ac86`，对照[原验收](review-2026-09-18-futures-basis-acceptance.md)与[修复回应](review-2026-09-18-futures-basis-p2-response.md)。

**结论：F1—F4 关闭，阶段一软件整改通过。** 复验另发现 1 项 P3（官方参照的排除清单与实际引用矛盾），已在本次一并修复并补回归；不影响已提交的 v3 证据。

**亚洲决策时点误差仍为 UNIDENTIFIED，决策级验收仍未通过。** 本次没有执行第二阶段验证协议，也没有改变估算公式、排名、采集时间或持久化结构。

## F1—F4 复验

| 问题 | 复验方式 | 判定 |
|---|---|---|
| F1 有效交易日数 | 读 `block_bootstrap_ci`：门槛改为每个键的非空会话数；抽样只在至少一个键达标时生成；含空结果或 `None` 的重采样整体降级为 `INCOMPLETE_RESAMPLES`、`ci=null`，不再丢弃后出区间。共同起点比较单独用同一批完整会话（各期限长度相同，未触发对齐异常）。30/1 天、全空期限、空抽样三项回归通过 | 关闭 |
| F2 官方参照完整性 | 选作参照的 Nasdaq 响应逐条核对来源、端点、HTTP/error 与 body SHA256；哈希错误整次拒绝且不写输出；导出 msg_id、源文件、hash、校验状态、接收时刻、契约版本。原全零 hash 反例现退出 1 | 关闭 |
| F3 响应身份 | 分析前要求期货为 `<contract>.CME`、指数为 `^NDX`；NQU26.CME、NQ=F、空串、null、^GSPC 均返回 INSTRUMENT_MISMATCH 且不写输出 | 关闭 |
| F4 跨整点读取 | 写入器关闭后把已轮转的 `.jsonl` 解析为 `.jsonl.gz`，缺段报 RAW_SEGMENT_MISSING；跨小时、跨 UTC 日期两种 FakeFetcher 回归均完成重解析与哈希校验 | 关闭 |

原验收的复现脚本已改为执行正确行为回归，本次运行 17 项全部通过。

## 新发现并已修复

### G1 · P3：带行级解析问题的官方响应同时出现在"已排除"和"已引用"中

位置：`tools/futures_basis_intraday.py`，`load_official_closes()`。

`parse_nasdaq()` 对单行坏日期只记 issue、继续解析其余行。886ac86 看到任一 issue 就把整条报文写入 `excluded_responses`（reason `PARSE_ISSUES`），但没有 `continue`，其余行照常进入官方参照并能把收盘判为 `VERIFIED`。

**复现：** 一条官方响应含 `09/17/2026` 有效行和一条 `date="bad"` 行。输出中同一 msg_id 既在 `responses`，又在 `excluded_responses`；09-17 诊断为 `VERIFIED`，`ref` 指向这条"已排除"的报文。

**影响：** 数值本身无误（坏行确实没进入参照），但来源说明自相矛盾，复查者无法判断该报文是否被使用。已提交 v3 的 `excluded_responses` 为空，7 个来源响应都没有行级问题，不受影响。

**修复：** 报文整体无可用记录（如 symbol 不是 NDX、JSON 结构异常）时才列入排除清单，并带上 issue 明细；仍有可用行时保留为已引用来源，行级问题记在该来源的 `row_parse_issues` 字段（没有问题时不输出该字段，因此 v3 的输出结构不变）。新增两项回归：部分坏行的报文只出现在 `responses` 并带问题明细；symbol 错误的报文只出现在排除清单且不能判 `VERIFIED`。前者在修复前失败，修复后通过。

## v3 证据核对

| 检查 | 结果 |
|---|---|
| v1 SHA256 | `70c6370f…aa7f`，与记录一致 |
| v2 SHA256 | `7a067362…95e5`，与记录一致 |
| v2 → v3 逐字段比较 | 仅以下字段变化：2h/3h/6h 的 bootstrap `sessions`（22→21/16/9，两种统计量）、bootstrap 说明、`metric_version`、`schema_version`、`generated_utc`；新增 `same_start_comparison.bootstrap` 与官方来源 `responses`/`excluded_responses`。跨会话样本、盘中窗口统计、ACF、收盘诊断、旧版对比均相同，与回应一致 |
| v3 区间 | 四个期限及共同起点比较（9 个会话）全部 `INSUFFICIENT_SESSIONS`、`ci=null` |
| v3 官方来源 | 7 个已校验响应；22 个收盘日引用其中 3 个，全部可回溯到已校验来源；无冲突、无排除 |
| 决策字段 | `UNIDENTIFIED` / `null` / `false` 不变 |

## 本次验证

| 检查 | 结果 |
|---|---|
| `uv run pytest` | 327 passed（886ac86 为 325，另加上述 2 项） |
| 研究模块及工具两套测试 | 83 passed |
| `uv run lint-imports` | 1 kept，0 broken |
| `uv run ruff check src tests tools/futures_basis_intraday.py` | All checks passed |
| 原验收复现脚本 | 17 passed |

## 未能在本环境复验的项

复验在云端容器中进行，没有 `~/qdii-data` 生产原始日志，因此以下项目沿用 886ac86 回应的记录，本次**未独立复现**：

- 用固定 run_id 从原始 Yahoo 日志离线重算 v3；
- 09-18 输入包回放与流回放（1138/1138）；
- 本机 `http://127.0.0.1:8787` 是否已加载新的决策披露字段（原验收时尚未部署）。

请在本机依次运行：修复回应中的重算命令（输出到新路径后与 v3 比较，预期除 `generated_utc` 外相同）、`uv run qdii replay relative --date 2026-09-18` 及加 `--stream`。G1 的修复只在出现行级问题时增加字段，v3 的 7 个来源都没有行级问题，重算结果不应因本次改动而变化。

## 验收边界

| 验收项 | 判定 |
|---|---|
| F1—F4 | 关闭 |
| G1（P3，本次发现） | 已修复并回归 |
| 阶段一软件整改 | 通过 |
| 生产数据重算与回放 | 以本机复跑为准（见上节） |
| 运行中服务的披露字段 | 未部署；后续发布时核对页面、JSON、下载信封是否为同一版本 |
| 亚洲误差界限与决策级验收 | 仍未通过；下一步需按[验证协议](../phase0/futures/asian-decision-validation-protocol.md)取得独立参照，以及用户的阈值、容差、误判率与置信水平 |
