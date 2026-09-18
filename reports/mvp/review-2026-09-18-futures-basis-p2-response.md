# 期货基差研究四项 P2 修复与复验

日期：2026-09-18。修复基线：`0cb2b06`。依据：[验收报告](review-2026-09-18-futures-basis-acceptance.md)。

**四项 P2 已修复并通过针对性回归，阶段一软件整改通过。亚洲决策时点误差仍未知，决策级验收仍未通过。**

## 修复内容

| 问题 | 修改及拒绝条件 | 回归证据 |
|---|---|---|
| F1 有效交易日数 | bootstrap 按各期限非空会话计数，空占位不能通过 30 日门槛；共同起点比较单独使用共同有效会话；若重采样包含空结果则返回 INCOMPLETE_RESAMPLES、ci=null，不静默删除后输出区间 | 30 个对齐日而 6h 只有 1 日：返回 INSUFFICIENT_SESSIONS/1/null；全空期限返回 0/null；共同会话不足时全部期限拒绝给区间 |
| F2 官方参照完整性 | 验证 Nasdaq 来源、端点、HTTP/error、body SHA256；损坏响应明确失败且不写输出，非预期来源或失败响应记录排除原因；导出路径、hash、校验状态、接收时刻、契约版本与 msg_id | 官方参照全零 hash 被拒绝；来源/端点错误、HTTP 503、带 error 的响应不能晋级 VERIFIED；压缩日志来源可追溯 |
| F3 响应身份 | 分析前要求期货响应 symbol 等于请求的 `<contract>.CME`，指数等于 `^NDX`；身份不符返回 INSTRUMENT_MISMATCH，不写输出 | NQU26.CME、连续 NQ=F、空/null 身份及错误指数均拒绝 |
| F4 跨整点读取 | writer 关闭后解析最终段路径，识别已轮转的 .jsonl.gz；缺段返回 RAW_SEGMENT_MISSING | 模拟跨小时、跨 UTC 日期，均完成落盘、压缩、重解析与哈希校验 |

新增 17 项参数展开后的回归。四项主缺陷的新增用例在修复前均失败，修复后通过；空重采样用例也先失败再通过。研究代码没有改变生产 E-NAV 公式、排名、采集时间、输入包或持久化快照结构。

## v3 证据

研究 schema 升为 `futures-basis-evidence-3`，metric 为 `futures-basis-research-3`。新文件：[futures-basis-2026-09-18-v3.json](evidence/futures-basis-2026-09-18-v3.json)。v1/v2 保持字节不变。

```text
uv run python tools/futures_basis_intraday.py NQZ26 \
  --raw-file /Users/weizhang/qdii-data/research/raw/yahoo/2026-09-18/06.jsonl \
  --run-id RESEARCH-20260918T062540Z \
  --output <新的输出路径.json> \
  --official-close-root /Users/weizhang/qdii-data \
  --legacy reports/mvp/evidence/futures-basis-2026-09-18.json
```

与 v2 的对比：

- 21 个跨会话样本及统计、普通/正式收盘诊断、盘中窗口及统计、ACF、旧版比较结果均完全相同。
- bootstrap 有效交易日数改为 1h=22、2h=21、3h=16、6h=9；v2 曾把每项都写成 22。区间仍全部为 null。
- 共同起点比较使用相同的 9 个完整会话，新添的 bootstrap 也全部为 INSUFFICIENT_SESSIONS/null。
- 官方参照导出 7 个通过校验的候选响应；22 个收盘日期最终引用其中 3 个，所有引用均可解析回相应已校验来源。保留全部候选来源以便复查冲突判定。
- `asia_decision_error_status=UNIDENTIFIED`、`asia_decision_error_bound_bp=null`、`decision_grade=false` 保持不变。

历史文件 SHA256：

| 文件 | SHA256 |
|---|---|
| v1 | `70c6370f4216287d2f43bd2c1d883a252ca1f5c9aeadf43fa368a0675e78aa7f` |
| v2 | `7a0673625ed75540313d0cc86d6dbaf6c6cba2557fc17cdec45dbbd7297995e5` |

## 本次验证结果

| 检查 | 结果 |
|---|---|
| `uv run pytest` | 325 passed |
| 研究模块及工具两套测试 | 81 passed |
| `uv run lint-imports` | 1 kept，0 broken |
| `uv run ruff check src tests tools/futures_basis_intraday.py` | All checks passed |
| 独立代码复核与四项原始反例 | 四项根因已修复，未发现新的可行动缺陷；随后追加的空抽样保护亦由回归验证 |
| 固定原始 run_id 离线重算 | 成功；主统计与 v2 相同，统计元数据及来源信息按上述规则更新 |
| 09-18 输入包回放 | PASSED，1138/1138，mismatch=0，version_changes=0 |
| 09-18 流回放 | PASSED，1138/1138，mismatch=0，version_changes=0 |
| 09-18 决策快照回放 | NO_DATA，0 条，退出码 3；不计为通过 |

原验收复现脚本已改为调用正式正确行为回归，执行入口不变：

```text
uv run python reports/mvp/evidence/futures-basis-acceptance-probes-2026-09-18.py
```

## 保留的边界

本次关闭四项实现缺陷，不关闭亚洲时点经济误差、结算锚点语义或总误差未知。未执行第二阶段验证协议。

没有重启或部署采集器、改 launchd、推送代码或交易。运行进程需要在后续发布时加载新代码；本报告不声称在线状态页已经更新。生产决策快照仍无当日样本。
