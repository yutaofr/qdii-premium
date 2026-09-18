# 期货基差复审整改实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. 若执行环境没有该技能，按下列步骤执行并记录验证结果。

**Goal:** 修正 f237d46 的时间配对与经济推论，保留可复现的探索性研究，在产品中明确亚洲决策时点误差未经验证，并制定独立验证协议。

**Architecture:** 研究计算拆为可离线验证的纯函数与薄 I/O 工具；生产估算公式保持现有代理语义。产品输出独立披露“有估算”和“通过决策验证”两个维度；后续验证协议独立于研究样本统计。

**Tech Stack:** Python 3.12、uv、pytest、现有 exchange_calendars/CalendarProvider、现有 rawlog、ruff、import-linter。第一阶段无需新增数据供应商或统计依赖。

---

## 0. 范围、核验依据与推荐方案

仓库：`/Users/weizhang/w/qdii-premium`。核验基线：`f237d46a8d83c8d3030f09fc71e9ebb047bced66`。2026-09-18 开始核验时工作区干净。本文件是计划，尚未执行代码整改。

推荐分两阶段推进：第一阶段完成任务 1—6，可验收“整改与探索性工具”；第二阶段实际执行任务 6 的研究协议，独立申请决策级验收。第一阶段不能顺带宣称第二阶段完成。

其他选择：仅改报告最快，但留有时间错配和产品误读风险；直接建立亚洲公允价值模型可继续研究，但需新数据与可识别性论证，不能作为这次修复的前置条件。

已核实：

- `tools/futures_basis_intraday.py:81-103` 按日期取配对序列首尾，且盘中统计采用重叠窗口。
- `reports/mvp/acceptance.md:75` 和 `reports/phase0/futures/findings.md:146-192` 存在过度推论。
- 原始日志 `/Users/weizhang/qdii-data/research/raw/yahoo/2026-09-18/06.jsonl`，run_id=`RESEARCH-20260918T062540Z`，两个响应体 SHA256 验证通过。
- 09-17 NQ 的 15:55/16:00 close 分别为 29744.50/29737.25；NDX 的 15:55/16:00 值分别为 29442.1171875/29446.98046875。22 天中 21 天的 NDX 末条为 15:55，仅最后一天另有 16:00 记录。
- `src/qdii/apps/relative_snapshot.py:125` 的 `absolute_premium_available` 目前表示存在 PROXY_ANCHOR 估算，并非已通过绝对买入判断验证。当前没有已实现的用户买入阈值引擎；不要借此次修复扩建交易系统。
- 本次只核对代码与原始样本，未重新运行全量测试；引用评审所称测试通过时必须注明来源。

执行约束：读取执行时实际存在的 AGENTS.md/CLAUDE.md，检查当前 HEAD 和工作区，不覆盖其他人的修改。保留原始日志、历史 JSON 和生产快照。可按任务做本地提交，不推送、不部署、不改 launchd、不发通知、不交易。用户使用场景仍是巴黎早晨、临近 A 股收盘才开机或唤醒，不要求夜间开机。

## 1. 先固定经济定义

设同合约、同口径的 `b(s)=F(s)/I(s)-1`，`R_F=F(t)/F(c)`、`R_I=I(t)/I(c)`：

```text
R_F / R_I - 1 = (b(t)-b(c)) / (1+b(c))
R_F - R_I     = R_I * (b(t)-b(c)) / (1+b(c))
```

因此“基差漂移”“收益率差”“净值相对误差”应分字段，不直接画等号。亚洲时点的 `I(t)` 若指经济公允价值，需要定义独立基准；不能把停留在昨收的官方指数值当成正在更新的经济价值。

生产用结算 `S(c)` 作分母，还存在锚点差异：

```text
[F(t)/S(c)] / [I(t)/I(c)]
    = [(1+b(t))/(1+b(c))] * [F(c)/S(c)]
```

这只是分解，不证明任一项已知。研究期货 bar close、生产 midpoint、官方结算、供应商昨结算是不同定义。

方向回归算例：`b(c)=0.01, b(t)=0.0095`，基差收窄 5bp，期货代理净值相对偏低约 4.9505bp；假设真实溢价 10%，估算溢价偏高约 5.4482bp。该合成算例仅验证符号，不证明亚洲时点存在这种变化。

CME 当前说明对主力合约列出 14:59:30—15:00 CT 的成交 VWAP 及无成交回退规则，其他月份另有规则，不能概括为每份合约的结算都是单一时刻成交价。参考 [CME Nasdaq-100 settlement](https://cmegroupclientsite.atlassian.net/wiki/spaces/EPICSANDBOX/pages/457222172/Nasdaq-100) 与 [2020 时间调整通知](https://www.cmegroup.com/notices/ser/2020/09/SER-8591.pdf)。这些材料不证明新浪字段的日期与值已获官方确认。

## Task 1：撤回超出证据的结论

**修改：**
- `reports/mvp/acceptance.md` 第 5 节误差范围行、第 6 节相关限制。
- `reports/phase0/futures/findings.md` 第 9—10 节。
- `tools/futures_basis_intraday.py` 文档字符串、注释和输出文案。
- `src/qdii/core/enav.py` 中把结算描述为同刻精确成交价的注释，仅修正文档语义。

**步骤：**
1. acceptance 恢复“亚洲决策时点期货段误差：未知；决策级验收未通过”。跨日三日样本仍可保留，但写成样本内结果，不写总体上界。
2. findings 第 9 节改为“Yahoo 美股现金时段与跨会话端点基差变化：探索性证据”。保留旧结果的研究性质与版本，新的数字必须来自任务 4 的重算。
3. 撤回“包络”“均值回复已证明”“不随时间增长”“亚洲方向为负”“系统性低估 0.02—0.05 个百分点”“总误差小于 0.1 个百分点”“不会影响买入”。说明原方向推导亦错误，不替换成另一项未经观测的亚洲方向结论。
4. 撤回“b(c) 每天精确已知”，区分 bar close、正式指数收盘和结算锚点；在第 10 节明确扩大端点样本不能关闭亚洲误差未知。
5. 新建 `reports/mvp/review-2026-09-18-futures-basis-response.md`，逐项回应评审、记录符号问题和未关闭事项。

**验证：** 检索旧断言，区分历史引述与当前有效结论；核对报告之间不再相互矛盾。文档修改无需为固定句子新增脆弱测试。

## Task 2：明确时间语义，修复端点选择

**新增：** `src/qdii/core/futures_basis_research.py`、`tests/unit/test_futures_basis_research.py`、`tests/fixtures/recorded/yahoo_basis_endpoints_20260917.json`。

**修改：** `tools/futures_basis_intraday.py`。

**先写失败测试，再实现：**

1. 建立最小价格记录结构：合约/指数身份、原时间戳、`interval_start`、`interval_end`、`price_kind`、close、来源引用。普通 5m bar 和额外正式收盘记录使用不同类型；无法确认语义的记录标为 UNKNOWN 并排除，不通过价格相近猜类型。
2. 从上述原始响应抽取最小 fixture，并带父响应 hash、msg_id、提取说明；合成覆盖边界的 fixture 必须标 synthetic。
3. 主研究序列统一为 `BAR_CLOSE_TO_FIRST_BAR_CLOSE`：在交易日历确定的现金会话内，普通线按区间终点配对；常规日收盘只用 `[15:55,16:00]` 普通线，次会话起点只用 `[09:30,09:35]` 普通线，命名“前会话最后完整五分钟线收盘 → 下一会话首根五分钟线收盘”。NDX 的普通线 close 不宣称等于正式收盘值。
4. 把 NDX 16:00 额外记录从普通线序列隔离。可单列 `OFFICIAL_CLOSE_TO_FIRST_BAR_CLOSE` 诊断：只有正式收盘来源/类型已核实，才把该值与结束于现金收盘的期货 bar close 配对；不可把 NQ 16:00—16:05 bar 搭配 NDX 16:00 正式收盘。没有足够官方记录时输出不足，不混入主序列。
5. 日历调度放在工具 I/O 层，调用现有 `CalendarProvider`；纯函数接收已确定的会话开闭时刻。覆盖 DST、提前收盘、节假日、日历不确定。不得以某日最后可用值替代缺失收盘。
6. 只有相邻的预期交易会话端点齐全才构建跨会话样本。缺整天、缺首根、缺末根都记录排除原因；缺周二不能把周一到周三标成正常隔夜。
7. 不完整当前 bar、HTTP/JSON 错误、null、非正数、非有限数、冲突重复时间戳、空序列、单会话均有明确处理。禁止 `overnight[-1]` 在零样本时崩溃；有效跨会话样本为零时返回 `NO_DATA`。

**最低测试矩阵：**
- 15:55=29744.50、16:00=29737.25：收盘期货价格只能选 29744.50；正式收盘诊断的 NDX 值为 29446.98046875，主普通线序列则为 29442.1171875，两口径不能混同。
- 下一会话开盘记录实际终点为 09:35；报告时长以区间终点计算，不从标签直接相减。
- 13:00 提前收盘、冬夏时 UTC 映射、周末/假日、缺中间交易日、未知日历、当前半根线、空/单会话。
- 非法值与重复值质量状态。

**命令：** `uv run pytest tests/unit/test_futures_basis_research.py`。先确认用例暴露旧算法问题，再确认修复通过。

## Task 3：统计只回答可观测问题

**修改：** 同任务 2 的纯函数模块、测试、工具。

1. 保留未舍入的 signed basis drift 和绝对值；只在展示层舍入。另报 `relative_return_error_bp` 与 `return_difference_bp`，按第 1 节公式计算。
2. 分位算法固定为 nearest-rank：排序后取 `ceil(p*n)-1`；报告 `quantile_method`、n、有效交易日数、排序位置。21 个样本的 P95 是第 20 个，不能充当总体 P95 的精确估计或上界。
3. 跨会话按 NORMAL/WEEKEND/HOLIDAY_EXTENDED 分层，附真实 elapsed hours 和具体假日/缺数原因；假日优先于纯周末分类，不能仅按小时阈值猜。缺失会话属于数据缺口，不归入假日。
4. 各期限先按同一会话开盘终点固定起点，选择完整、互不重叠的窗口（相邻窗口可共享一个边界点）；缺任一内部 bar 时排除该窗口，不前向填充。
5. 主表报告 signed mean、绝对值 median/P95/max、signed variance、窗口数与天数；同时报各期限使用的起点时段分布。另报同一批完整会话、相同开盘起点的 1/2/3/6 小时比较，避免不同时段覆盖混淆。
6. 以交易日整块重采样，保留日内依赖；固定随机种子和重复次数（默认 2000）。期限比较共享同一批重采样日期。少于 30 个有效会话时默认 `ci_status=INSUFFICIENT_SESSIONS`、CI=null；30 只是保守的软件展示门槛，不能宣称统计充分。若另列小样本 bootstrap 诊断，必须标低稳定性且不用于验收。
7. 如展示 ACF，限定连续会话内，明确输入是基差水平或去日均值水平，报告 lag、配对数、日期数；常数/不足样本输出 null。不得把日间拼接造出的相关性或相近 P95 自动解释为均值回复。
8. 固定输出：`asia_decision_error_status=UNIDENTIFIED`、`asia_decision_error_bound_bp=null`、`decision_grade=false`。样本再多、P95 再小也不能由此升级。

**测试：** 精确分位数位置；舍入前统计；窗口不重叠与缺口排除；日聚类保留关联；重采样可重现；常数 ACF；第 1 节方向算例；端点 5bp、区间中间 20bp 的反例不得产生亚洲界限。

**命令：** `uv run pytest tests/unit/test_futures_basis_research.py`。

## Task 4：离线重算与证据版本

**新增：** `tests/unit/test_futures_basis_tool.py`。

**修改：** 工具；新增重算 JSON，保留原 `reports/mvp/evidence/futures-basis-2026-09-18.json` 不变。

实现 CLI：

```text
uv run python tools/futures_basis_intraday.py NQZ26 \
  --raw-file /Users/weizhang/qdii-data/research/raw/yahoo/2026-09-18/06.jsonl \
  --run-id RESEARCH-20260918T062540Z \
  --output reports/mvp/evidence/futures-basis-2026-09-18-v2.json
```

1. `--raw-file` 模式禁止网络请求，按 run_id/endpoint 明确选两个响应，验证 body SHA256；重复匹配不能随意取最后一条。
2. 网络模式继续先写原始日志，再重解析；使用 finally 关闭 writer。不得用新抓取的数据替代旧样本冒充重算。
3. JSON 包含 schema/metric 版本、时间配对规则、合约、原始路径/msg_id/hash/received_at、日历版本、分位算法、排除明细、统计、未知状态、limitations。
4. 文件已存在时默认拒绝覆盖；网络输出文件名包含 run_id 或时间，避免同日覆盖。旧证据在回应文档中标 `superseded for interpretation`，不篡改历史文件。
5. CLI 测试覆盖离线无网络、hash 错误、缺响应、空样本、单会话、输出冲突。同输入的分析内容应一致，生成时刻等元数据单列。
6. 生成旧/新结果对比，解释变化来自端点、分位算法、精度还是筛选；原来 21 个跨会话值可能不变，但不能预写结论。修正最后一天诊断和原 1.9bp 供应商比较；不把修正后的价差叫供应商误差上界。

**命令：** `uv run pytest tests/unit/test_futures_basis_tool.py tests/unit/test_futures_basis_research.py`，随后执行上面的离线命令并检查输出。

## Task 5：产品明确“有估算，但未通过绝对决策验证”

**修改：** `src/qdii/apps/relative_snapshot.py`、`src/qdii/apps/status.py`。

**测试：** 扩充 `tests/unit/test_enav.py`；需要时新增 `tests/unit/test_enav_disclosure.py`。

本阶段优先在视图/导出 envelope 增加披露，不改数值模型、排名、采集时间和核心持久化 schema。避免仅因展示变化导致旧生产快照无法复算。

每只基金与组级视图显式增加：

```json
{
  "estimate_available": true,
  "absolute_decision_eligible": false,
  "absolute_decision_status": "UNVALIDATED_ERROR",
  "absolute_error_bound_bp": null,
  "decision_limitation_codes": ["ASIA_FUTURES_ERROR_UNIDENTIFIED"]
}
```

语义：PROXY_ANCHOR 对应当前代理数值可用且决策未验证；REFERENCE 对应历史参考；UNAVAILABLE 对应无估算。组级状态不得把“任一成员可用”变成全部可判断。第一阶段不存在 eligible=true 的路径。

1. 同步页面、CLI 文本、机器 JSON 和 `full_bundle()` 下载 envelope；原 snapshot 与 bundle 内容/哈希保持不变。测试导出新增字段与原快照完整性兼容。
2. 保留 `absolute_premium_available` 作为兼容别名，文档明确它只表示存在代理估算、已弃用；新消费者使用两个独立字段。先搜索全部消费者，确保没有买入判断依赖旧布尔值。
3. 用户文案：“估算溢价可供参考；亚洲决策时点的期货代理误差尚未验证，当前不能据此确认是否满足买入条件。” 不显示“可买入”或由 5bp 推导的安全区间。
4. 相对排名和差分情景区间照常工作，但说明不能替代绝对估值验证。高溢价可作为描述，不能作为普遍无误判的证明。
5. UI/JSON/下载同一状态测试；接近阈值和远离阈值的数值都不能解除 UNKNOWN；缺输入、REFERENCE、冷启动、北京 14:50 唤醒保持原行为。
6. 若实现发现必须改动核心持久化结果，先在本地补齐 schema/版本方案和测试，再实施；旧快照单列 VERSION_CHANGED 与完整性检查，不重写。默认路线无需此变更。

**命令：** `uv run pytest tests/unit/test_enav.py tests/unit/test_relative_snapshot_replay.py tests/unit/test_collector_wake.py`；若新增披露测试也运行该文件。渲染一份合成状态页检查用户看到的含义，不运行或重启生产采集。

## Task 6：独立验证协议与最终验收

**新增：** `reports/phase0/futures/asian-decision-validation-protocol.md`。

协议必须回答以下问题，当前没数据的部分明确写“未取得”，不可为了结案填默认数字：

1. **估计目标：** 实际使用时刻的可执行卖一相对经济公允净值的误差；并拆出期货代理段。固定上海 14:50/14:55 等观测点，记录 UTC、当地会话和真实触发时刻，正确换算巴黎冬夏时。
2. **独立参照与可识别性：** 是否能获得同刻、足够新鲜、具有实际覆盖度的独立持仓估值？列明供应商、价格类型、持仓/权重覆盖、缺失/停牌/现金处理与参照自身误差。QQQ 扩展时段、另一期货源、IOPV 或次日开盘均不得自动作为真值。由同一个 NQ 构造的公允价值不能反过来验证 NQ。
3. **若没有独立参照：** 结论保持 UNIDENTIFIED；可研究持有成本/股息/融资模型与多代理交叉检验，但产物叫模型敏感性或一致性研究。次日兑现误差混入之后的市场变化，只能另设预测目标。
4. **生产 as-of：** 输入实际接收时间不晚于决策截止；固定月份合约、当时可得锚点、来源与 raw hash 完整留存。历史补采、供应商最终修订、当时在线观察分层，不能互相替代。
5. **实验设计：** 按交易日为基本单元，预先规定普通/周末/假日/换月/压力层；明确压力定义与样本数、缺数选择偏差、序列依赖和覆盖不足。训练/校准与时间向前的样本外验证隔离；不得自动以 30/60/100 天作为充分验收。
6. **验收阈值：** 先明确用户买入阈值、额外安全边际、可容忍误判率与置信水平，再设计需要的精度/样本数。不在这次整改中擅自选择阈值或宣称 P95 足够安全；P95 即使有效仍有尾部风险。
7. **不下结论区间：** 当前误差不可界定，绝对买入条件保持无法判定。未来若可验证总净值相对误差 `e=V_hat/V_true-1 ∈ [l,u]`，其中 `l>-1`，则真实溢价区间为：

   ```text
   p_low  = (1+p_hat)*(1+l)-1
   p_high = (1+p_hat)*(1+u)-1
   ```

   对最大可接受溢价 T 与非负安全边际 m，`p_high < T-m` 才能确认满足该溢价条件；`p_low > T+m` 才能确认超过该阈值；其余不下结论。等号留在不下结论区间。满足溢价条件不等同于完整投资建议。若 [l,u] 是概率区间，必须同时披露覆盖概率与适用范围；情景假设区间不得标成验证区间。
8. **误差合成：** 同口径分解/传播净值误差与价格误差，覆盖期货、结算锚点、FX、仓位/现金/跟踪偏差、价格可执行性、舍入与参照误差；不能相加几个样本 P95 冒充总体 P95 或上界。存在未知项时总误差仍未知。

**最终验证命令：**

```text
uv run pytest
uv run lint-imports
uv run ruff check src tests tools/futures_basis_intraday.py
uv run qdii replay relative --date 2026-09-18 --data-root /Users/weizhang/qdii-data
uv run qdii replay relative --date 2026-09-18 --data-root /Users/weizhang/qdii-data --stream
uv run qdii replay relative --date 2026-09-18 --data-root /Users/weizhang/qdii-data --kind decisions
```

回放只读核验已有样本：记录条数、模式、版本与状态；NO_DATA 不算通过。遇到历史版本差异按已有机制报告，不为通过检查回写生产数据。基线命令本来只 lint `src tests`，此次必须单独覆盖被修改的工具。

**阶段一验收表：**

| 项目 | 关闭条件 |
|---|---|
| P1 目标替换、方向推论、总误差/买入结论 | 文档与产品输出均撤回，机器字段不能误示决策通过 |
| P2 端点错配 | 明确区间语义、普通线/正式收盘分离、日历与缺口回归通过 |
| P2 均值回复推论 | 撤回强推论；非重叠/按日统计与限制可复现 |
| 原始证据与重算 | 新版结果有来源/hash/规则/排除记录，历史未改写 |
| 生产兼容 | 数值、排名、冷启动、回放按各自证据核验 |
| 亚洲误差界限、决策级验收 | **仍未通过，不能随阶段一关闭** |

最后提交回应文档，列出实际改动、测试输出、重算差异、仍未知事项。不以“所有测试绿了”替代经济验证。

## 可直接交给 Claude Code 的执行指令

```text
在 /Users/weizhang/w/qdii-premium 执行
docs/plans/2026-09-18-futures-basis-review-remediation.md。

先核对当前 HEAD、工作区及 AGENTS.md/CLAUDE.md，再依序完成任务 1—6。
本次目标是关闭 f237d46 的报告/统计/时间语义问题，交付探索性工具整改、
产品决策资格披露和独立验证协议。不是宣布亚洲时点误差已知。

重点：
1. 撤回约 5bp、总误差小于 0.1 个百分点及不影响买入的决策级推论。
2. 修正基差收窄的代数方向，但不生成相反的亚洲经验结论。
3. 区分普通五分钟线与正式收盘记录，禁止全体时间戳统一平移。
4. 固定原始 run_id 离线重算，保留旧证据；实现非重叠、按日统计。
5. 页面/CLI/JSON/下载明确：估算可用不等于通过决策验证；未知界限用 null。
6. 保持生产公式、相对排名、现有快照与早晨唤醒使用方式兼容。

每项代码变更先写能暴露问题的测试，再实现并运行针对性检查。
最后执行计划中的全量测试、架构检查、ruff、离线重算和只读回放。
允许本地分步提交；不推送、不部署、不重启采集、不交易。
完成后给出评审逐项回应、新旧数值差异、实际验证结果、未关闭的经济问题。
若独立亚洲参照拿不到，把原因写入协议，继续完成其他授权整改。
```
