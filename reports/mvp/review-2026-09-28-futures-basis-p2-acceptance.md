# 期货基差四项 P2 修复复验：886ac86

日期：2026-09-28。范围：`0cb2b06..886ac86`，对照[原验收](review-2026-09-18-futures-basis-acceptance.md)与[修复回应](review-2026-09-18-futures-basis-p2-response.md)。

**结论：F1—F4 关闭，阶段一软件整改通过。** 复验另发现 2 项问题，均已修复并补回归：G1（P3，官方参照的排除清单与实际引用矛盾）和 G2（P2，官方参照没有知识截止，本机重算无法复现 v3）。G2 改变了官方来源清单的口径，研究证据升为 schema 4，需在本机重新生成 v4 并做两次重算的一致性检查（见文末）。v3 的数值结论（22 条收盘记录、`analysis`、`legacy_comparison`）经本机重算确认不变。

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
| `uv run pytest` | 328 passed（886ac86 为 325；G1 新增 2 项，G2 新增 1 项并修改 1 项） |
| 研究模块及工具两套测试 | 84 passed |
| `uv run lint-imports` | 1 kept，0 broken |
| `uv run ruff check src tests tools/futures_basis_intraday.py` | All checks passed |
| 原验收复现脚本 | 17 passed |

## 本机复跑结果（2026-09-28，提交 `6f3fd83`）

云端容器没有 `~/qdii-data`，以下由用户在本机用生产原始日志运行：

| 检查 | 结果 |
|---|---|
| 修复回应中的离线重算命令 | 退出 0 |
| 与 v3 比较（忽略 `generated_utc`） | **未通过**：`official_closes.responses` 7→17、`records` 22→23、`excluded_responses` 0→1 |
| 差异归因（用户核对） | 全部来自 09-21—09-28 新增的 Nasdaq 日志：多 10 个响应、1 条 09-18 收盘记录、1 个 HTTP 超时响应；另有 1 个旧响应所在段从 `.jsonl` 压缩为 `.jsonl.gz`，路径改变。原 22 条收盘记录、`analysis`、`legacy_comparison` 完全一致；新旧响应都没有 `row_parse_issues` |
| 09-18 输入包回放 | PASSED，1138/1138，mismatch=0，version_changes=0，退出 0 |
| 09-18 流回放 | PASSED，1138/1138，mismatch=0，version_changes=0，退出 0 |

回放复验通过。重算的数值部分通过，但证据文件不可复现，归为 G2。

### G2 · P2：官方参照没有知识截止，数据目录增长会改变证据

位置：`tools/futures_basis_intraday.py`，`load_official_closes()` 与 `build_document()`。

886ac86 按收盘日期 `[first, last]` 筛选记录，但扫描 `--official-close-root` 下全部 Nasdaq 日志，没有按接收时刻截断。生产采集每天追加日志，同一 run_id 的离线重算因此随运行日期而变：来源清单变长；日期窗口末日（09-18）的收盘在之后才入库，也会被加进来并参与收盘诊断；之后出现的超时或损坏报文还会进入排除清单，甚至让整次重算以 HASH_MISMATCH 失败。另外，`raw_file` 记录实际文件名，段被轮转压缩后同一报文的路径会改变。

v3 本身也受影响：它在 09-18 18:32Z 生成，来源清单中有一条 06:30:15Z 接收的响应，晚于研究响应的接收时刻 06:25:40Z（该响应没有被任何收盘记录引用，数值不受影响）。

**修复：**

- 知识截止取研究响应（期货、指数两条中较早者）的接收时刻，与分析使用的 as-of 相同；只读接收时刻不晚于它的官方报文，之后的段整段跳过（段按接收时刻的 UTC 日期/小时命名），同一小时段内晚到的报文逐条跳过。截止之后的报文不列入来源或排除清单，也不做哈希检查。
- 输出记录 `knowledge_cutoff_utc_ns`、`knowledge_cutoff_utc` 与 `cutoff_rule`。
- `raw_file` 记逻辑段路径（`.jsonl`），压缩为 `.jsonl.gz` 后不变；读取仍兼容两种文件。
- 研究 schema 升为 `futures-basis-evidence-4`；统计口径 `futures-basis-research-3` 不变。v1—v3 保持字节不变。

**回归：** 先运行一次，再追加截止之后的日志（同一小时段内的晚到报文、次日新收盘、超时响应、哈希损坏报文）并压缩早段，第二次运行的 `official_closes` 与收盘诊断必须与第一次完全相同；来源导出测试改为断言逻辑段路径。两项在修复前失败、修复后通过。

**对 v3 的预期影响**（按 v3 已记录的来源推算，需本机 v4 确认）：22 条收盘记录及其引用的 3 个响应都早于截止，`records`、`analysis`、`legacy_comparison` 应与 v3 相同；来源清单从 7 条变为 6 条（去掉 06:30:15Z 那条未被引用的响应）；6 条来源的 `raw_file` 从 `.jsonl.gz` 变为 `.jsonl`；新增截止字段，`schema_version` 为 4。

## 验收边界

| 验收项 | 判定 |
|---|---|
| F1—F4 | 关闭 |
| G1（P3，本次发现） | 已修复并回归 |
| G2（P2，本机复跑发现） | 已修复并回归；v4 证据待本机生成 |
| 阶段一软件整改 | 通过 |
| 09-18 两种回放 | 本机通过，1138/1138 |
| 生产数据重算 | 数值部分与 v3 一致；可复现性待 v4 两次重算确认 |
| 运行中服务的披露字段 | 未部署；后续发布时核对页面、JSON、下载信封是否为同一版本 |
| 亚洲误差界限与决策级验收 | 仍未通过；下一步需按[验证协议](../phase0/futures/asian-decision-validation-protocol.md)取得独立参照，以及用户的阈值、容差、误判率与置信水平 |

## 待本机执行：生成 v4 并确认可复现

用本提交重新生成 v4，写入 `reports/mvp/evidence/futures-basis-2026-09-18-v4.json`；再输出到另一路径重算一次，两次比较应只差 `meta.generated_utc`。v4 与 v3 的差异应只在上文"对 v3 的预期影响"所列范围内。
