# 评审处理记录：MVP 架构与用户决策评审（2026-09-15）

对应评审：[review-2026-09-15.md](review-2026-09-15.md)（基线 `b60293c`）。处理人：架构（Claude）。
处理方式：先在基线上逐条复现，再修复并补回归测试；验收报告据实改写。

## 总体结论

**接受评审结论。** 撤回验收报告中"软件不再阻塞，只剩 G0/G1"的表述。当前定位为 **相对比较研究预览**：它只回答"五只中谁相对便宜"，不回答"当前价格比估算价值贵多少"。页面与命令行都明确声明，绝对溢价是否满足买入条件无法据此判断。用户"依据实时溢价判断是否买入"的最终目标需要 E 路径（绝对估算溢价），积累会话数不会让 R 自动具备这项能力。

## 逐条处理

| 编号 | 复现（基线） | 结论 | 修复 | 回归测试 |
|---|---|---|---|---|
| R1 · P1 | 错位 0 秒与 59 秒边界相同，均为 ROBUST | **确认** | 边界 = 日终历史差分 P95×√k + 报价错位情景项 z·σ·√((Δt+1s)/年化秒数)（VM-11，σ=20%、6.5 小时集中、z=2）+ 净值舍入；错位超过 15 秒只给模型参考（TIME_SKEW）。页面在每个结论旁显示差值与边界分项，并声明"未经盘中实测，超出情景边界不等于统计显著"；"差异明确"改为"超出情景边界" | `test_r1_skew_widens_bound_and_large_skew_is_not_robust`、`test_skew_term_matches_vm11_scenario`、`test_pair_bounds_resolve_or_not` |
| R2 · P1 | 同日同值净值后补分红字段，仍判已核验 | **确认** | 以完整经济事实（单位净值、累计净值、增长率、事件字段）去重；任一事实带事件字段 → CORPORATE_ACTION_PENDING；同日数值或增长率被修订 → PENDING_VERIFY；只隔离对应基金 | `test_r2_same_value_nav_with_later_event_field_isolated`、`test_at46_…` |
| R3 · P1 | 接收较晚、行情较旧的报文覆盖了新价 | **确认** | 按基金维护最新市场快照，以（供应商快照时间，接收时间）比较新旧；较旧行情只计入审计计数；不完整批次不抹去其他基金的有效值；无时间的行情不进入比较 | `test_r3_late_arriving_older_quote_does_not_override`、`test_r3_single_member_regression_and_incomplete_batch`、`test_out_of_order_snapshot_does_not_rewind` |
| R4 · P1 | 一只基金先更新净值，五只全部退出 | **确认** | ① 采集跨日归一化因子：Nasdaq 官方 NDX 日收盘（I(a)）与中国货币网当日中间价（X0，L0_FX_T），写入输入包；② 日期不一致时，有因子的成员用完整公式比较；没有因子时取最大的同日期子集（同数取较新日期），其余成员单独退出 | `test_r4_one_fund_newer_nav_keeps_other_four_comparable`、`test_r4_with_normalization_factors_all_funds_compare_across_dates`、`test_r4_long_lagging_single_fund_excluded_not_group`、`test_r4_state_supplies_normalization_factors_across_dates` |
| R5 · P2 | 零样本回放返回成功、退出码 0；1290 点证据在 /tmp | **确认** | 回放状态分为 PASSED / FAILED / NO_DATA / VERSION_CHANGED；NO_DATA 退出码 3，失败 1；报告记录快照目录与已验证条数。持久证据写入 `~/qdii-data/evidence/mvp/replay-2026-09-15/`，摘要见 [evidence/replay-2026-09-15.json](evidence/replay-2026-09-15.json)，可用 `tools/mvp_replay_evidence.py` 重跑。流回放改为对每条快照在其截止时刻重建，同时适用于决策快照 | `test_r5_empty_replay_is_no_data_not_success`、`test_store_and_bundle_replay_detects_tampering`、`test_collector_writes_replayable_relative_snapshots` |
| R6 · P2 | 2027-01-04 构包抛出 DateOutOfBounds | **确认** | 截止日或锚点日不在日历覆盖范围内时，不查询交易日区间，输入包记 `calendar_covered=false`，所有成员以 CALENDAR_UNCERTAIN 退出，组状态 INELIGIBLE | `test_r6_calendar_out_of_range_degrades_without_exception` |
| R6 附 · AT58/60 | 覆盖文件无校验，也无回退保护 | **确认，不以"停机重载"豁免** | 覆盖文件严格校验（未知键、市场、日期、来源、竞价时段），任一非法则整份不加载，所有市场视为不确定；已激活休市台账 `state/calendar_closures.json` 拒绝删除已知临时休市的回退版本，受影响市场不确定且台账不被改写；采集器启动时写 CALENDAR_UNCERTAIN 事件 | `test_at58_corrupt_override_not_half_loaded`、`test_at58_unknown_keys_or_missing_source_rejected`、`test_at60_rollback_removing_known_closure_rejected`、`test_valid_closure_override_applies_and_is_ledgered` |

## 口径澄清的处理

| 口径 | 处理 |
|---|---|
| 1 官方净值对照口径随比较模式变化 | 固定为 最新价 / 已披露单位净值 − 1，与比较口径（卖一价）分开；输入包新增 `last_price`；页面列标题注明口径并显示最新价（`test_nav_premium_uses_last_price_not_ask`） |
| 2 网页拿不到完整输入包 | 新增 `/relative/bundle.json`（规范输入包 + 结果 + 质量，含成对边界与来源），页面提供下载链接；`qdii relative --json` 同样输出完整内容 |
| 3 页面 bundle_id 不在快照库 | 页面明确标注"按请求时刻即时生成，未写入快照库"，并显示最近持久化快照 ID；新增 `qdii relative --save`，写入 `snapshots/decisions/`，可用 `qdii replay relative --kind decisions [--stream]` 校验（`test_decision_snapshot_saved_and_stream_verified`） |
| 4 NFR01 缺逐点 P95 | 2026-09-15 重新生成的 1290 个触发点：单点耗时（解析 + 构包 + 计算 + 写盘）P50 2.0 ms、**P95 2.3 ms**、最大 8.2 ms（限值 1000 ms）；该数据是回放测得，不是生产在线测量 |

## 验证

- 回放：9 月 15 日重新生成的 1290 个快照，输入包重算与流回放均 PASSED（1290/1290）。
- 实机：采集器已用新代码重启。`/relative.json` 显示收盘参考，成对边界含错位项（3.9—5.4 bp，错位 1—4 秒）；页面显示范围声明与边界分项；`/relative/bundle.json` 为 schema 2。
- **生产写盘尚未验证**：快照只在采集窗口内写入，下一个窗口是 2026-09-16 巴黎 07:30。窗口结束后应执行：

```bash
uv run qdii replay relative --date 2026-09-16 && uv run qdii replay relative --date 2026-09-16 --stream
```

## 仍未解决

1. **E 路径（用户最终目标）：** 需要美股收盘 NQ 锚点或可信补采源，当前主机窗口无法实时捕获（ADD-0 §15.2）。建议下一步把用户目标写成验收场景：选定 ETF → 有效卖一 → 带时间与误差说明的 E-NAV → 买入绝对估算溢价 → 用户阈值 → 可判断 / 数据不足。
2. **差分边界的盘中适用性：** 需要用积累的窗口会话实测相邻快照的相对价差漂移，以校准或推翻 √k 外推与错位情景项。
3. **G1 官方净值抽查：** 每只基金至少 3 个日期，尚未完成。
4. **G0：** 需要能力矩阵与明确的产品路径决定，不只是累计会话数。
