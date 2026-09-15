# QDII ETF数据源契约规格

文档ID：DS；版本：1.0；对应SRD：1.3。DS-01—DS-12为规则组，实际适配器另保存contract_version及证据哈希。以下接口是待持续核验的候选，不是供应商SLA；原探测记录移至评审与证据附录。首版不要求同时实现全部候选或自动切源。


## DS-01 可用性分级

| 标记 | 含义 |
|---|---|
| 文档确认 | 公开文档或源码描述了接口，尚不代表本环境调用成功 |
| 样本可取 | 本次获得返回值；只证明连接及部分解析可行 |
| 盘中验证 | 连续实际交易时段验证事件时间、更新频率、语义及缺失率 |
| 实时合格 | 盘中验证通过，并有足够依据确认无已知供应商延迟；可进入实时状态 |

v1.0探测在北京时间2026年9月15日凌晨进行，A股已经收市。不能以这些样本证明A股盘中实时性；也未完成任何免费NQ源的全时段实时认证。

## DS-02 来源清单

| 数据 | 首选免费路径 | 备选或对照 | 原探测核验及限制（不代表今天可用） |
|---|---|---|---|
| ETF价格、盘口 | 新浪公开行情批量接口 | 腾讯行情；东方财富 | 三类入口已返回样本；分钟及秒级质量仍需盘中验证 |
| 官方单位净值历史 | 管理人官网，解析披露日期 | 天天基金历史净值接口 | 官网页可查询；聚合源需与管理人抽查一致 |
| 官方IOPV | 交易所或管理人公开披露链路 | 明确标注IOPV的行情供应商 | 各ETF逐日核验；缺失不阻止自估模式 |
| NDX历史收盘 | 新浪美股指数历史，AKShare适配 | Nasdaq官方指数页面核对 | `.NDX`接口有文档；不得误用`.IXIC`纳斯达克综合指数 |
| NQ盘中价格 | 新浪`hf_NQ`候选入口 | 已有权限的真实合约源；CME延迟行情核验 | 已取样，但真实月份、口径及延迟未验证；默认实验状态 |
| USD/CNY即期 | 中国货币网CFETS公开即期报价 | 新浪在岸报价 | CFETS适配文档存在；新浪样本含“计算得出”说明，不能当作直接成交价 |
| USD/CNH即期 | 新浪离岸报价候选 | 其他具有明确时间戳的免费源 | 样本可取；仅在岸缺失时作为显式代理 |
| 人民币中间价 | 国家外汇管理局／中国货币网 | 管理人披露估值汇率 | 日频数据，不能标为盘中即期 |
| 基金规则与事件 | 管理人公告、基金合同、招募说明书、PCF及报告 | 交易所披露 | 必须保留文件生效日期与获知时间 |

接口依据：[AKShare基金文档](https://akshare.akfamily.xyz/data/fund/fund_public.html)、[指数文档](https://akshare.akfamily.xyz/data/index/index.html)、[期货文档](https://akshare.akfamily.xyz/data/futures/futures.html)、[外汇文档](https://akshare.akfamily.xyz/data/fx/fx.html)。AKShare是开源适配库，既不是交易所授权证明，也不会把上游延迟数据变成实时数据。

## DS-03 ETF报价接口契约

以下为本次实际访问的公开入口示例，不视为供应商保证长期兼容的API。

```text
GET https://hq.sinajs.cn/list=sh513100,sz159696,sz159501,sz159660,sh513390
GET https://qt.gtimg.cn/q=sh513100,sz159696,sz159501,sz159660,sh513390
GET https://push2.eastmoney.com/api/qt/stock/get?secid=1.513100&fields=f43,f57,f58,f59,f60,f86
```

新浪按GB18030解码。针对本次A股格式，赋值字符串以逗号分隔、下标从0开始：0名称，1今开，2昨收，3最新，6买一价，7卖一价，8成交量，9成交额；10与11是买一量及价，20与21是卖一量及价，30日期，31供应商时间。长度、代码、价格精度和单位必须验证；其他市场类型不得复用此映射。

腾讯本次格式以`~`分隔：1名称、2代码、3最新、9买一价、10买一量、19卖一价、20卖一量、30供应商时间。仅用于已验证的A股格式，量纲须与另一来源交叉核验后才能用来测算成交金额。

东方财富本次返回：`f43=2191`、`f59=3`，解析价格为`2191/10^3=2.191`；`f57`代码、`f58`名称、`f60`昨收、`f86`供应商时间字段。不能把`f86`未经验证地命名为最后成交时间。本次请求额外盘口字段并未返回盘口，故此接口只验收已取得的字段；缺盘口时不虚构买卖价。

供应商时间可能在15:00后继续更新，必须分别存储`provider_snapshot_time`与`last_trade_time`。不能用报文更新时间把收盘价格包装成收盘后新成交。

## DS-04 净值与指数接口契约

```python
# 可用于开发适配验证，调用不是可用性承诺
ak.fund_etf_fund_info_em(
    fund="513100", start_date="20250101", end_date="20260914"
)
ak.index_us_stock_sina(symbol=".NDX")
```

天天基金历史净值候选入口：

```text
GET https://api.fund.eastmoney.com/f10/lsjz?fundCode=513100&pageIndex=1&pageSize=30
Referer: https://fundf10.eastmoney.com/
```

需检查HTTP状态、JSON业务状态、`Data.LSJZList`是否存在；通常字段`FSRQ`是净值日期、`DWJZ`是单位净值、`LJJZ`是累计净值，具体以保存的返回样本校验。所有日期必须作为数据处理，不根据抓取日硬编码为T-1或T-2。若字段缺失或语义变更，接口失败，不输出0。

历史数据进入系统时记录`first_seen_at`。只有净值日期、没有历史发布时间的数据可以用于研究，但不能假装早在净值日期当天已经可知。[Nasdaq官方NDX说明](https://indexes.nasdaqomx.com/Index/Overview/NDX)用于指数身份核对；是否能免费取得完整官方历史需单独检查。

## DS-05 期货接口契约与真实性约束

```text
GET https://hq.sinajs.cn/list=hf_NQ
```

AKShare提供`futures_foreign_commodity_realtime`及可订阅代码查询；适配前确认代码表是否包含NQ。直接报文与适配库必须保留原始日期、时间、价格类型及品种身份，不能只保留“涨跌幅”。

本次读取的AKShare新浪期货适配源码将字段0映射为最新价、2与3为买卖价、6为时间、7为昨日结算价、12为日期、13为名称；并用昨日结算价计算所输出的涨跌幅。这直接说明：该涨跌幅不能不经调整就接在美股现货收盘之后。该适配还计算“人民币报价”，本项目仅取美元指数点因子，自行处理FX，禁止再乘一次人民币报价隐含汇率。源码版本在开发锁定时保存内容指纹。[新浪期货适配源码](https://github.com/akfamily/akshare/blob/main/akshare/futures/futures_hq_sina.py)。

必须识别：交易所、真实合约月份、到期日、报价为成交还是买卖中价、是否连续／复权／计算序列、是否存在已知延迟。`NQ`、`NQ1!`、`NQ=F`之类聚合符号不自动等于固定月份合约。

真实CME E-mini NQ的最小变动单位为0.25点，合约乘数为20美元／点。官方网页明确提供延迟行情，不能作为本项目的免费实时主源。[CME产品说明](https://www.cmegroup.com/markets/equities/nasdaq/e-mini-nasdaq-100.html)。价格粒度检查只适用于声称是成交价的数据；买卖中价可能落在0.125点，连续调整序列还可能有其他精度。

若只能取得聚合或计算序列，可运行`provenance_confidence=PROXY`估算，但必须标注“期货代理，口径未完成验证”，禁止进入高可信告警。启用绝对估算的每个来源分别保存美股现货收盘时的同序列锚点；换月不明或序列跳变时停止该绝对估算。相对M0是否可用另行判断。

## DS-06 汇率接口契约

```text
GET https://www.chinamoney.com.cn/r/cms/www/chinamoney/data/fx/rfx-sp-quot.json
GET https://hq.sinajs.cn/list=fx_susdcny,fx_susdcnh
```

中国货币网的接口路径可在[AKShare配置源码](https://github.com/akfamily/akshare/blob/main/akshare/fx/cons.py)核对；其适配实现使用POST并读取`records`，包含`ccyPair`、`bidPrc`、`askPrc`、`time`等字段。当前源码在输出表格时丢弃时间，所以正式适配必须保留原始字段，而不能以抓取时间替代。[适配源码](https://github.com/akfamily/akshare/blob/main/akshare/fx/fx_quote.py)。GET和POST是否接受、返回时间属于何时区，均须按运行环境验收。

即期主值使用同一快照中有效买卖价的中点；若只有单边或供应商计算参考价，注明类型并降级。跨源切换不得静默拼接不同口径的绝对价。中间价从[外汇局历史入口](https://www.safe.gov.cn/safe/rmbhlzjj/index.html)读取，核对是否按100美元报价，统一换算成每1美元。

## DS-07 刷新与故障策略

仅对已启用指标所需来源执行以下轮询；R模式不要求启动NQ或即期FX采集。相同数据由多个界面使用时复用现有采集结果。

- 正常交易时：ETF、NQ每5秒尝试刷新；即期汇率每10秒。目标可调整为3—15秒，前提是来源允许且实际更新有意义。
- 五只ETF批量查询，共用一个NQ和一个汇率快照；多个前端不增加上游请求数。
- 净值每15分钟查询或依据已知披露窗口查询；公告、PCF在盘前及盘中按合理频率更新。
- 超时或限流后按5、15、30、60秒退避；HTTP 403、429或验证码按来源要求停止／退避，不绕过访问限制。
- 单源失败不删除已保存数据；计算状态根据年龄自动降级。首版仅启用已验证的单源，故障时降级并保留旧快照；自动备源切换为后续能力，启用时须整对切换当前行情及锚点，并通过连续3个推进的有效样本检查。


## DS-08 访问条件与Phase 0探测记录

对于新浪Referer等条件，逐入口保存无凭据的最小请求模板、请求方法、字符集、响应样本哈希、状态码、日期、实际延迟证据及适用范围。Phase 0验证正常官方页面使用的请求头是否是公开接入必要条件；不将“2022年以来一定要求Referer”当作本项目实测事实。

填写普通协议字段不自动等于获得调用许可；只有符合来源允许的正常访问路径才能使用。不得伪造付费权益、登录身份、授权令牌，或在403、验证码、封禁后轮换身份及地址绕过限制。若只有不被允许的方式可获取，该源不通过。遵守许可也包括录制和历史保留范围；哈希只能证明内容一致，不能代替缺失的原始数据完成回放。

IOPV候选路径具体限定为：①ETF管理人PCF／基金披露页中的IOPV发布说明；②沪深ETF行情中名称明确的IOPV字段；③已说明该字段来自交易所或管理人链路的免费行情供应商。Phase 0逐基金记录是否有值、字段、来源链、计算及更新时间口径、免费访问条件。当前没有经验证的五基金通用IOPV接口，不能把PCF静态参考值或供应商自估值改名为官方IOPV。缺失不阻断相对比较。

## DS-09 NQ身份、换月与锚点可恢复性

CME当前公开惯例表列出2026年9月到期9月18日、换月9月14日，规则是到期月份第三个周五之前的周一；不是评审所称9月10日。市场参与者可自行择日换月，供应商连续序列也可能另有切换规则。原探测在换月周是事实，但不能由惯例日期确定hf_NQ已变成Z26。[CME惯例换月表](https://www.cmegroup.com/trading/equity-index/rolldates.html)。

验证分三层：明确固定合约身份 → 可验证的序列构造及切换记录 → 只有数值跳变猜测。只有第一层或能还原固定合约链的明确规则支持经验证绝对估算；识别疑似跳变仅能触发暂停，不能证明没有未识别切换，也不能凭检测器自动修复。

每个启用源的锚点键至少包括`source_id, series_id, contract_id, price_type, c_utc, contract_version`。仅支持一条聚合代码时，不能宣称同时录制了两个真实月份。参考版可以在连续性证据足够的区间运行代理，身份或换月连续性无法支持时该区间绝对结果null。

补采优先级：同源同月份事件历史 → 已验证等价来源的同月份历史（明确记录来源变化） → 用户合法导入的同语义历史。日结算价、日K收盘或c之后的下一分钟不得替代c。候选补采源必须在Phase 0真正取得至少一条故意漏掉的c样本并验证时间标签；只列URL不算完成。当前尚无已验证补采源。

同源完整10分钟延迟历史也能补采c，只能在实际收到后使用；不要求c时就无延迟。无法证明两源价格序列可比时，不能跨源补洞。缺失最近c的NQ锚点会阻止标准绝对链，通常要等下一合格锚点或补采恢复；不必停掉独立相对比较，也不能绝对断言整天所有功能不可用。

## DS-10 时钟、日历与运行窗口

统一保存UTC，展示默认Asia/Shanghai；现货美股使用America/New_York，NQ按实际交易所日历时区转换。保存event_time及其精度、provider_snapshot_time、received_at；来源只有时分秒但日期未知时不猜测日期。公告announced_at不等于系统received_at。

参考时钟为主机操作系统时间同步服务报告的可信NTP同步状态及偏移，记录检查时间；无法取得偏移时标CLOCK_UNVERIFIED，不能声称已满足2秒目标。偏移绝对值超过2秒标CLOCK_SKEW；事件时间晚于received_at超过2秒先隔离，不从该异常反推服务器准确。超时和间隔使用单调计时，时钟回拨不能倒退行情指针。回放使用原始输入包固定的计算时刻，不读今天的墙上时钟。

日历首版采用锁定版本的现成库或核验本地文件，另加维护者覆盖文件。`exchange_calendars`公开目录列有XSHG，`pandas_market_calendars`列有SSE；本次证据不足以确认所选版本具备独立XSHE或完整NQ产品日历。Phase 0必须实际列出支持项、覆盖年限、午休、提前收盘及临时休市处理；不存在的名称不得假装支持。沪深相同安排可有证据地复用，但保留各自身份和例外。[exchange_calendars官方仓库](https://github.com/gerrymanoim/exchange_calendars)、[pandas_market_calendars官方日历目录](https://pandas-market-calendars.readthedocs.io/en/latest/calendars.html)。

覆盖文件至少包含市场、日期、开闭市／暂停、原因、官方来源、received_at及版本哈希。首版可停止采集→检查→加载完整版本→重启；不要求热更新、自动仲裁或管理界面。已知冲突及覆盖缺口直接标CALENDAR_UNCERTAIN，由维护者解决，不继续按旧规则出可信结果。网络失联但本地适用覆盖有效时正常启动，标同步降级；不承诺离线副本包含未知突发事件。基线必须覆盖当前、所需历史以及已公布的跨年安排，禁止按周一到周五补造缺口。

保留calendar_version、tzdb_version及解析后的UTC边界。历史快照不随库升级重算；时区规则会因政策改变更新，不能只固定日历包版本。[IANA时区数据库](https://www.iana.org/time-zones)。

首版核心ETF窗口为沪深实际交易日9:30—11:30及13:00—15:00，集合竞价、午休及停牌独立标识。采集绝对链需覆盖每个实际美股收盘c前5分钟至后2分钟，提前收盘从日历求出，不能只设北京时间04:00／05:00。已知延迟源额外持续至c+已知延迟+2分钟；未知延迟时不假设两分钟足够。在c+2分钟检查直接采集缺口、记录状态；之后按源延迟及补采策略更新，不能将有延迟的到达当作当时已收到。

相对比较版无需NQ夜间采集，只需在使用前获取完整历史锚点。绝对版要求运行环境在所需窗口可靠唤醒并联网；可用7×24常驻主机，也可用有可验证唤醒能力的计划任务，SRD不强制所有时段高频抓取。手机可读是界面适配，不自动推导出公网服务；如启用远程访问，必须采用受控访问，部署方式留给ADD。

界面持续显示最近成功c、合约、各源锚点及缺口、补采结果。采集器在故障发生时写状态，用户打开页面即可看到，不等09:30才开始检测。09:15运行盘前就绪检查。浏览器关闭时的软件内提醒无法保证送达；无人值守外部通知属可选后续功能，本轮不配置或发送任何消息。

## DS-11 最小可复现数据对象

下列是数据语义，不要求特定数据库或完整通用双时态查询引擎。首版保存版本化记录及不可变计算输入包即可；缺少历史原始知识时间的导入数据只能用于研究。

| 对象 | 最少字段／约束 |
|---|---|
| FundProfile | id、exchange、code、factor_group、currency、benchmark、index_variant、nav_fx_rule、model_policy、effective_from/to、received_at、source、revision |
| NavRecord | id、code、nav_date、nav_type、currency、unit_nav、first_seen_at、received_at、source、revision、raw_hash、supersedes_id |
| NavValidation | nav_record_id、status、recorded_at、verified_at、reason_codes、evidence_ids、policy_version；验证状态事件追加保存 |
| MarketQuote | id、symbol、source_id、series_id、contract_id、price_type、value、unit、event_time、time_resolution、received_at、contract_version、raw_hash |
| QuoteSide | snapshot_id、side、raw_price/volume、price/volume、side_state、reason_codes、contract_version；无效侧价量null |
| FutureIdentity | id、source_id、vendor_symbol、exchange、contract、expiry、tick_size、series_type、delay_status、declared_delay_seconds、received_at、effective_from、revision |
| FuturesAnchor | id、source_id、series_id、contract_id、price_type、c_utc、quote_id、received_at、contract_version、origin（LIVE/BACKFILL/IMPORT） |
| FundAnchor | nav_record_id、validation_id、a_utc、index_quote_id、fx_quote_id、included_event_ids |
| FundEvent | id、code、event_type、effective_at、announced_at、received_at、terms、source、revision |
| Exposure | id、code、weights/betas、as_of_date、received_at、training_cutoff、source、revision；报告日不冒充获知日 |
| RuleSnapshot | id、calendar_version、tzdb_version、resolved_utc_sessions、policy_version、source_contract_versions、received_at、activated_at |
| InputBundle | id、target_time、knowledge_cutoff、computed_at、input_ids、rule_snapshot_id、model_version、price_basis、factor_group；固定时钟及规则 |
| ValuationSnapshot | bundle_id、relative_result、estimates及各自quality、premiums、market_phase、book_state、reasons；不以新结果覆盖旧记录 |
| PCFFxReference（后续） | code、pcf_date、revision、received_at、reference_time、pair、unit、value、purpose、evidence |

同一事实的修订不得覆盖原记录。选择时同时满足事实生效时点≤目标时点、received_at≤knowledge_cutoff，净值还需verified_at≤cutoff；规则必须在截止时刻已激活。对账和研究可另算“按最新信息重算”，结果类别与实时录制分开。无需先建设任意历史条件查询平台。

## DS-12 录制与离线回放的首个交付物

Phase 0先交付可运行的最小录制回放工具：保存允许保留的原始报文或可重算的标准字段、接收时间、来源／契约版本、内容哈希；支持从保存的输入包离线调用估值纯函数。合成夹具与实采会话明确分开，不能将注入故障归为供应商实际行为。

至少回放一次真实盘中会话和一次合成故障集，覆盖零盘口、时间倒退、漏锚点、修订及缺日历。同一输入包输出数值与原因必须一致；输入被保留策略删除时标注不可完全回放。不要求首版实现分布式消息队列、自动乱序重排或任意查询引擎。
