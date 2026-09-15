# SRD 1.3 文档集勘误（冻结前）

适用：SRD 1.3、DS 1.0、VM 1.0、QS 1.0。日期：2026-09-15。
性质：以下均为一致性或遗漏修正，不新增功能需求、不改变 P0 数量。建议合并为 SRD 1.3.1 / QS 1.0.1 / VM 1.0.1 后冻结，此后需求变更只由 Phase 0 证据驱动。

| ID | 位置 | 问题 | 修订建议 | 影响的 AT |
|---|---|---|---|---|
| E1 | QS-02 输出策略表 | R 要求各 ETF `age_wall ≤ 60 秒`。15:00 之后与午休期间所有 ETF 报价都会超过 60 秒，R 整体不可用；"晚上查看明天买哪只"无法得到结果 | 新增策略 `CLOSING_REFERENCE`：当 ETF 的 `market_phase ∈ {BREAK, CLOSED}` 时，以该阶段开始前最后一个有效连续交易快照为输入，ETF 价格的 `freshness=NOT_APPLICABLE`，显示快照时刻与阶段；该结果不得标为当前可交易，也不参与机会通知 | 新增 AT79：15:30 查看，五只收盘快照齐全 → 输出 CLOSING_REFERENCE 相对结果，显示 15:00 快照时刻，`opportunity_alert_allowed=false` |
| E2 | QS-08 目标 JSON；DS-11 ValuationSnapshot | 相对结果是组级与成对数据，却挂在单基金记录中（`benchmark_code`）；单只基金样例的状态写成 `UNRESOLVED` + `DIFFERENCE_UNRESOLVED`，应为无准入 | 输出拆为 `GroupSnapshot{bundle_id, factor_group, price_basis, policy, members[{code, S, rank, eligibility, reason_codes}], pairs[{i, j, R, delta, relative_status, reason_codes}]}` 与 `FundSnapshot{…绝对估值…}`；DS-11 中 ValuationSnapshot 同步拆分；单基金无成对时 `relative_status=INELIGIBLE` | AT61—AT63、AT78 断言对象改为 GroupSnapshot |
| E3 | SRD §3 PH0-06、QS-01 | 按 QS-01，X₀ 未知时默认 R 不可用，因此五只基金 FX 条款核验是 GO-R 的关键路径，但未标注 | ① PH0-06 注明"R 关键路径，Phase 0 首日开始"。② 可选放宽：当比较双方锚点日 a 相同、候选 FX 规则为有限集合时，将规则不确定性作为有上界的差分误差项（取候选规则下 X₀ 比值的最大偏差），`relative_status` 最高为 MODEL_REFERENCE，不得为 ROBUST_DIFFERENCE | 若采纳②，新增一条对应 AT |
| E4 | SRD §3、§7 AT71/AT76/AT77 | "P0"同时表示 FR 优先级、Phase 0 任务编号（P0-01…）和 AT 范围列（"P0/L"） | Phase 0 任务改称 PH0-01—PH0-08；AT 范围列改为 `PH0`、`PH0/L` | AT71、AT76、AT77 |
| E5 | SRD §7 AT21、AT36 | 标为 R/E，但断言内容只对 E 成立；R 不依赖 NQ | AT36 拆分：E 分支维持原断言；R 分支断言"ETF 输入合格时 R 照常输出，不因 NQ 样本在未来而不可用"。AT21 的 R 分支断言"标明 ETF 午休冻结；R 可按 E1 输出午间参考" | AT21、AT36 |
| E6 | VM-10 准入条件 | "从各自锚点到 t 之间没有尚未纳入锚点的加法现金事件"未说明费用计提是否算在内；若算，所有基金都会被判 NONSEPARABLE_EVENT | 明确：管理费与托管费的逐日计提不视为加法现金事件，按 VM-11（约 0.164bp/日量级）计入差分误差；仅 ETF 自身派息、合并及其他一次性加法调整触发 NONSEPARABLE_EVENT | AT63 补一个"锚点滞后 2 日、费率不同"的准入用例 |
| E7 | QS-02 年龄定义 | `age_wall = computed_at − event_time`。`computed_at` 为计算完成时的墙钟，回放无法逐位复现，且计算耗时可能让 60 秒、120 秒边界翻转 | 改为 `age_wall = knowledge_cutoff − event_time`，其中 `knowledge_cutoff` 取触发本轮计算的消息的 `received_at`；`computed_at` 仅作元数据，不参与结果比较 | AT65、AT66 的年龄以 cutoff 为参照重述 |
| E8 | VM-03、DS 补采优先级（"日结算价不得替代 c"） | 维护者需求（2026-09-15）：在 A 股交易时段用纳指期货实时价算实时溢价。按 VM-03 必须在美股收盘时刻实采期货锚点，家用 Mac 夜间睡眠，首次实采即 MISSING，E 路径被主机开机状态阻断 | 以 hf_NQ 行情行自带的**昨结算**作 F(c) 的代理锚点：CME 股指期货日结算按美东 16:00 前 30 秒成交确定，与纳指收盘同刻；昨结算与买卖价在同一行情行，天然同一连续合约。结果标 `SETTLEMENT_ANCHOR_PROXY`，不标已验证。放行门槛：① 结算时刻与合约一致性（含提前收盘日、9/18 到期换月前后）；② 昨结算相对指数收盘的基差逐日符合持有成本（首版只做 −0.5%～+3% 合理范围检查，超出即不可用）；③ 行情行的昨结算已滚动到最近美股交易日（样本时间不早于 c+2 小时） | 新增：估算净值公式、缺输入/过期/基差异常各自不可用、昨结算 as-of 取样；AT72（结算价不得替代 c）对代理模式豁免并显式披露 |

## 附：本轮核对通过、无需修改的项

- VM-11 各量级：1.70/3.39bp、3.19/6.38bp、50.125/100.502bp、142.857bp、0.164bp/日。
- AT68：πE−πA = −1.089109 个百分点。
- VM-10 示例：1.10/1.12 − 1 ≈ −1.785714%。
- QS-02/QS-03 阈值与 AT65—AT67 一致。
- DS-09 的 CME 惯例换月日（2026-09-14，到期月第三个周五之前的周一）正确；此前评审所说的 9-10 为旧惯例，已撤回。

## 实现状态（2026-09-15）

| ID | 状态 | 位置 |
|---|---|---|
| E1 CLOSING_REFERENCE | 已实现（草案）：午休与收盘后使用阶段边界后 10 分钟内的冻结快照，或最后一个连续交易快照；按最新价/收盘价口径；机会提醒关闭 | `apps/relative_snapshot.py::is_reference_snapshot`，`tests/unit/test_relative_snapshot_replay.py` |
| E2 组级输出 | 已实现：`GroupResult`（成员 S、排名、退出原因 + 成对 R/δ/边界/状态），与单基金绝对估值分离 | `core/relative.py` |
| E3 X₀ 关键路径 | 已关闭：PH0-07 历史拟合 + PH0-06 招募说明书条款一致（估值日当日中间价），规则 VERIFIED；共同锚点下 X₀ 约掉 | `reports/phase0/history/…/findings.md`、`reports/phase0/fund_rules/…/findings.md`、`data/fund_rules/` |
| E6 费用不触发退出 | 已按此实现：只有来源事件字段或增长率断点才判 NONSEPARABLE_EVENT | `core/history_validation.py`、`apps/relative_snapshot.py::_latest_nav` |
| E7 年龄参照 cutoff | 相对比较按知识截止时刻计算年龄 | `core/relative.py` |
| E8 昨结算代理锚点 | 已实现（草案，未验证）：盘中估算净值与估算溢价；美股收盘实采窗口停用 | `core/enav.py`、`contracts/sina_hf_v2.py`、`contracts/cfets_fx_spot_v1.py`、`tests/unit/test_enav.py` |
