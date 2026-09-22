# 历史竞价回放、每日核验与因子实验

模块版本：1.5，适用于当前主系统。本文面向使用者、后续维护 AI 和外部研究程序；项目可位于任意可写目录。入口为主系统「策略回测」页面；本模块在本机计算，不需要 DeepSeek、OpenAI 或其他模型 Key。每日执行与外部 AI 的完整操作顺序另见 [持续算法优化手册](CONTINUOUS_OPTIMIZATION.md)。

目标是：**先固定历史日盘前已知的昨日涨停候选，用当时真实收到的竞价序列计算分数，再对照同一天收盘后的完整涨停池，最后按日期划分样本检验参数。** 当天结果只能作标签，不能倒流进候选、背景或竞价因子。

截至本次升级核查，本机历史竞价只有演示记录，没有可供七因子实证回测的真实竞价序列。已留存的十日涨停池可以计算相邻日期的延续基线，但不能据此声称现算法已获验证、已完成有效调参，或已有可靠预测准确率。软件提供回测能力与真实数据积累流程，不把能力上线写成策略效果通过。

## 日常使用

1. 交易日建议 09:05 前启动系统，09:10 前完成关注池准备。在「策略与接入 → 采集范围」勾选 **「自动收盘复盘与竞价核验」**，选择上海时间 15:10—15:59，默认 15:10。修改前先停止监测，点击「保存采集设置」后重新启动；保持电脑开机、不休眠。
2. 09:15—09:25 每个原始批次到达后立即保存、评分并发布。批次还记录当时权重，不等待十分钟收齐才分析。
3. 09:25 过后约一分钟（09:26 分钟）先自动写出实时排名视图 `data/research/daily/YYYY-MM-DD-morning-ranking.md`：直接使用每批到达后立即计算的引擎排名，列出名次、分数、待核验分、因子覆盖、数据阶段与是否为昨日涨停候选，并明确标注它是可刷新视图、不是不可变证据。它不创建也不改写冻结档案；若整个 09:26 分钟都被占用或进程未运行，09:27 的冻结 Markdown 仍给出同一排名与两个时点。
4. 两个固定时点保留评分证据，09:27 后自动整理档案：09:24:50 为盘前研究时点；09:26:00 为包含终态收尾的诊断时点。有当时引擎时点快照则保留原分，标 `method=recorded_ranking`；缺时点快照时，按原记录权重与所标识的引擎源码重放，标 `method=replayed`。两个时点的来源可不同，重放不能冒充原实时分数，也不是每批页面分数的日志。已冻结档案不会因之后调参而覆盖。同一次整理还会自动写出 `data/research/daily/YYYY-MM-DD-auction.md`，把两个时点的权重来源、覆盖、排名与缺失汇总成人读 Markdown；它与冻结 JSON 都只写一次，不需要再手动点「保存 Markdown」。
5. 当天收盘复盘完成后，用该日完整涨停池给档案加上「是／否／未知」标签。标签为该股是否属于收盘后取得的同日完整涨停池，不是盘中曾触板，也不是收益。没有合格结果池则保持未知。存在每日档案时，继续提交本机历史回放与参数实验。
6. 在「策略回测 → 每日原评分核验」选择日期及时点，查看冻结评分和收盘对照，保存 Markdown 或完整 JSON。在下方「实验记录」查看**按当前权重重新计算**的结果，与上方当日记录权重的档案区分。

自动流程要求系统保持运行，已核验官方交易日历和本日准备状态，且自动收盘复盘开启。停止服务或错过竞价时段不会补回未观察数据。手动生成收盘复盘也会尝试对应日核验；若缺原冻结清单、真实批次或原权重，明确显示不可用，不合成日档。

09:10—09:26（含整分钟）禁止启动历史实验、导入或参数建议归档，优先保障实时竞价。已有档案可继续阅读、下载。实验任务遇保护时段或服务退出会中止，已完成档案保留。

## 冻结档案的校正

每日档案是原始证据，正常调参、换权重、改公式都不重写它。只有确认当日**整日不可评分**源于已修复的归一化或实现缺陷（例如上游阶段命名不在显式白名单内），才允许生成一份独立校正版本：

```powershell
python tools/rebuild_daily_ranking.py --date 2026-09-21 --reason "上游阶段命名修复后按当日原批次重建评分"
```

校正规则：

- 工具只读本机 SQLite 的当日盘前清单、原始批次与批次内当时权重，不联网、不调用模型、不写 `batches`/`research_manifests`/`research_decisions`，因此不会制造可冒充原始证据的新批次或新时点决策。
- 输出是新的 `YYYY-MM-DD-<24位ID>.json/.md`，另附原冻结档案的 `YYYY-MM-DD-auction.md`；原 `YYYY-MM-DD-auction.json` 字节不变，只能用批次 SHA-256 对照。
- 档案内记录 `corrects_frozen_id`、`reason`、`supersedes`（原时点的方法、可评分数、引擎摘要）、`correction_source_sha256` 与 `batch_count`；两个时点仍按各自截止前最后一批合法权重重放，标 `method=replayed`。
- 09:27 前、没有冻结档案、原因短于 4 字或重放后仍无可评分记录时拒绝写入；重复执行同一数据与引擎返回同一版本，不产生重复文件。
- 15:10 后的收盘核验只在同一冻结档案的校正版本确有可评分行时使用重建分数，并记录 `scores_basis=correction`、`scores_source_id` 与原冻结 `auction_frozen_id`；否则维持原冻结分数。标签只加在结果上，不改写任何分数来源。

校正版本按当日原始批次与当时权重重建，不是当时页面已发布的原分，也不能用来把「当时没有观察」补成观察。`/api/research/daily` 与页面「每日原评分核验」会优先显示最近核验版本、其次是校正版本、最后才是冻结版本；三者在 Markdown 中都明确标注来源。

## 两类评分和研究范围

| 记录 | 使用的参数与时点 | 用途 |
| --- | --- | --- |
| 每日冻结档案 | 优先保留引擎时点快照原分；缺快照时用截止前记录的原权重重放，并标明来源 | 核对当日策略记录；保存七因子、质量与权重来源 |
| 当前参数实验 | 点击运行时捕获的当前权重；分别回放两时点 | 与原始证据对照，比较权重和因子贡献 |
| 涨停延续基线 | 相邻完整涨停池交集；可附前五个交易日出现次数、严格梯队分组 | 描述历史背景，不是竞价算法命中率 |

每日原权重缺失时，只能回退到当时冻结清单的权重，并带 `legacy_fallback` 提示；不得冒充已证明的原分数。同日权重变化次数另行保留，不把最后一组权重说成全天都在使用。

当前研究候选固定为上一交易日**完整涨停池**。前五日池提供背景，不用当日涨停结果挑选候选。实时采集仍支持自选、昨日涨停与趋势强股；自选只有实际采集的历史才存在于原始库，晚加入不能补回早前轨迹。当前这套实验不等于全部自选、趋势股或全 A 股的效果测试；不能将昨日涨停样本结论推广到这些范围。

每股代码保持六位及 `.SH/.SZ/.BJ` 后缀，保留前导零。上一交易日由留存交易日历确认，不用星期推断。盘前清单须在当日 09:15 及以前真实固定；迟到或事后恢复的上下文仅作回溯说明，不进入严格调参。未知、缺字段、无观察和无结果不是零，也不是预测失败。

## 如何比较与选择参数

实时引擎七因子公式继续沿用 [STRATEGY.md](STRATEGY.md)，本轮调权不改变其含义。回放只接受固定截止前本机真实接收的批次，按留存接收顺序逐批调用同一个 `AuctionEngine`，重新评分。09:26 结果不参与 09:24:50 的参数选择。

主要比较指标是每日按分数排序的前 `min(10, 有效样本数)` 只中，收盘仍在涨停池的比例，再对日期取均值。另提供合并股票日占比、样本池涨停比例、捕获比例和相对提升倍数。样本池比例为零时，提升倍数为空。描述性 Wilson 区间不消除同股、同日行情之间的相关性，不当作可靠的独立样本保证。

搜索顺序固定如下：

1. 显式排除演示、其他时点、日期重复、代码冲突、标签未知与不合格时点证据。所有候选参数使用相同的股票日集合：原基线可评分、七项因子分均为有限的 0—100 值。某因子权重降到零也不借机纳入缺该因子的股票。
2. 至少需要 30 个完整有效日期、300 个完整股票日。完整日要求输入代码唯一、所有标签已知，且至少有一个七因子完整的可评分股票；**不表示所有候选都已有完整竞价**，被排除股票和各因子缺失数仍披露。
3. 按日期排序，最后 20% 日期向上取整、至少 5 日作为保留检验期。较早的开发集正负结果各至少 30 个股票日；不能拿保留期标签补足搜索门槛。
4. 开发期采用三折按时间向前扩展的训练与后续验证。首次训练至少 10 日，每折验证至少 3 日。没有随机拆分股票，也不让同一天一部分股票在训练、另一部分在检验。
5. 固定随机种子 `20260921`，最多 64 组参数，包含当前权重、等权、移除单因子和有限扰动。每折只由较早训练期提名当前权重及至多两组其他候选；所有折都获提名者在共同验证日期比较。同分优先当前权重，再优先改动较小的参数。
6. 先冻结一组候选，再在保留日期评价一次。只有验证增益大于零、保留期日均前十涨停占比至少提高 3 个百分点、至少 60% 保留日期优于基线，才标为通过本次建议门槛。通过也不自动替换实时配置。

单因子实验与移除某因子的对照只在开发日期完成，保留期不用于反复挑因子。样本不足时 `candidate_weights=null`，继续保留原权重。基线表现可展示已有可用样本，但不足样本的好看数字不能越过门槛。

研究档案用数据、源码和基线权重摘要确定实验 ID；完全相同的实验复用既有档案。`holdouts.json` 记录已暴露的保留日期，其他实验重复使用这些日期时只作探索，状态为 `exploratory_holdout_reuse`，不得再称为新的独立验证，也不接受自动建议。看到保留期结果后改公式或权重，须积累新的未使用日期检验。

这里优化的是特定样本内的涨停排名识别能力，不是交易收益。不模拟手续费、滑点、停牌、涨停买不到或卖不出；分数不是概率。继续积累和检验数据可以改进证据，不能保证找到长期最优算法。

## 历史数据从哪里来

当前 [官方竞价接口](https://fuyao.aicubes.cn/docs/api-reference/auction/) 的 `auction/snapshot` 只有代码与 `stage=live/final`，没有历史日期参数。`data.timestamp` 是响应组装毫秒时间，不是交易所逐笔时间；回放的截止依据是实际本地接收／观察时间。按日期查询的短线风向标只有官方样本的竞价涨幅等字段，样本选择时点与完整性未足以证明全候选覆盖，不能替代七因子轨迹。

[涨停池接口](https://fuyao.aicubes.cn/docs/api-reference/limit-up-data/) 按显式日期完整翻页读取，用于前日背景和收盘结果。程序要求结果池在目标日 15:10 及以后取得，这是本地研究的时间门槛，**不是官网保证 15:10 后永不修订的终态承诺**。日线开盘价、当前快照、风向标与演示均不补造历史竞价。

已有本机真实批次直接读取；有权使用其他数据商历史序列时，可按下方契约导入。导入文件的时间、单位与当时可见性由提供者核实，程序只能验证结构和时间关系，不能独立证明外部来源的声明真实。

## 外部 JSON 导入契约

页面「导入已有历史竞价 → 下载空白格式模板」获取当前契约。模板为空，不带模拟行情。填写真实数据后选择文件导入，再运行本地回测。每次 HTTP 提交（含 `dataset` 外层）最大 **5 MiB**；每文件 1—40 个会话，研究导入目录合计最多 120 个交易日。每个会话最多 5,000 批，每批最多 100 条，完整池最多 6,000 股。超限不截断为完整样本。

下面是结构说明，尖括号字符串均为占位符，**不能直接导入**。数值占位须替换为真实 JSON 数字，缺值用 `null`；布尔项仅在事实成立时填 `true`。同一数组中的省略内容须填入所有实际记录，不是只提供示例行。

```json
{
  "schema_version": 1,
  "provenance": {
    "provider": "<合法历史数据提供者>",
    "dataset_id": "<来源批号或可核验数据版本>",
    "timestamp_semantics": "observed_at",
    "amount_unit": "CNY",
    "pct_unit": "percentage_points",
    "point_in_time_attested": "<已核实来源当时可见性后填布尔true>"
  },
  "sessions": [
    {
      "manifest": {
        "date": "<YYYY-MM-DD会话日>",
        "mode": "live",
        "prepared_at": "<会话日真实准备时间，ISO格式含+08:00>",
        "previous_date": "<前一交易日YYYY-MM-DD>",
        "calendar": ["<来源日历中的前一交易日>", "<会话日及其他真实日历日期>"],
        "context_complete": "<确认昨日全池完整后填布尔true>",
        "point_in_time": "<该清单当时真实可见填布尔true，否则false>",
        "codes": ["<当时采集范围内完整股票代码>"],
        "context": {
          "<完整股票代码>": {
            "thscode": "<与映射键相同的完整代码>",
            "name": "<股票名称>",
            "context_date": "<前一交易日YYYY-MM-DD>",
            "continue_day_cnt": "<来源计数或null>",
            "continue_day_text": "<来源连板原文>",
            "strict_consecutive": "<确证的严格连续板数或null>"
          }
        }
      },
      "batches": [
        {
          "received_at": "<会话日真实观察时间，ISO格式含+08:00>",
          "stage": "<live或final>",
          "data": {
            "timestamp": "<原响应组装时间毫秒数或null>",
            "auction_phase": "<原阶段字段，如order_entry/no_cancel/matched或兼容的live/final>",
            "data_status": "<原状态字段，如live/final或兼容的ready>",
            "item": [
              {
                "thscode": "<完整股票代码>",
                "name": "<股票名称>",
                "auction_price": "<真实价格或null>",
                "auction_pct": "<真实百分数原值或null>",
                "auction_amount": "<真实人民币元金额或null>",
                "auction_volume": "<原接口成交量字段或null>",
                "auction_turnover_pct": "<真实换手百分数原值或null>",
                "auction_volume_ratio": "<真实量比或null>"
              }
            ]
          }
        }
      ],
      "outcome": {
        "date": "<与会话日相同YYYY-MM-DD>",
        "complete": "<确认当日全池完整后填布尔true>",
        "retrieved_at": "<目标日15:10及以后实际取得结果池的ISO时间>",
        "rows": [{"thscode": "<当日涨停池完整股票代码>", "name": "<股票名称>"}]
      }
    }
  ]
}
```

`context` 必须包含昨日完整池，键与行内代码一致，不能只给今天继续涨停的股票。`codes` 则是当时实际采集范围，两者可能不同；未采到的昨日候选仍保留缺失记录。`continue_day_cnt` 不能自动当作严格连续板数；`5天4板` 不填成 4 连板。只有核验有连续事实才填写 `strict_consecutive`。

所有日期必须真实有效，前一交易日与来源日历一致。`prepared_at` 属于会话日；超过 09:15 或 `point_in_time=false` 会被标为事后重建，不进入优化。批次须按实际接收顺序排列，范围为会话日 09:15:00—09:26:00，不能把后补下载时间反写为早盘时间。终态在 09:25 前出现无效。收盘池取得时间不能来自未来，结果池不完整则拒绝导入；本机回放遇缺结果池时仍允许展示未知标签。

金额为元，`auction_pct`、`auction_turnover_pct` 为百分数原值（不是先除以 100 的比例），量比无量纲；成交量字段保留源契约单位，不以自行换算的手数冒充。七因子不使用未明确方向和单位的未匹配量推断买压。其他允许数值字段以 `engine.NUMERIC_FIELDS` 为准，未知字段不会透传为因子。不得上传 Key、请求头、账号或任意本机路径。

同日期已有本机实盘批次时，拒绝导入覆盖或混用；已有另一导入版本也拒绝覆盖。扩展超过 120 日应另建经过规划的研究目录或修改有界存储方案，不能删除旧证据来伪装全新保留样本。

## 外部 AI 与本机接口

主系统数值实验不调用模型。DeepSeek / ChatGPT 的普通盘面解释入口不自动承担参数优化；外部程序可以使用本机接口获取专用开发数据，再显式调用自己的模型 API。金融 Key 不用于模型，不需要在本机研究请求中携带任何 Key。

| 方法 | 本机路径 | 作用 |
| --- | --- | --- |
| GET | `/api/research` | 最近一次实验，未运行时 `status=not_run` |
| POST | `/api/research/run` | `{}`，启动 `research` 后台任务 |
| GET | `/api/research/template` | 空白 JSON 模板 |
| POST | `/api/research/import` | `{"dataset": 导入对象}` |
| GET | `/api/research/export?id=实验ID&format=markdown或json` | 导出固定版本的完整实验 |
| GET | `/api/research/daily` | `items` 每日档案目录 |
| GET | `/api/research/daily?date=YYYY-MM-DD` | 指定日冻结档案或收盘核验 |
| GET | `/api/research/daily/export?date=YYYY-MM-DD&format=markdown或json` | 每日档案下载 |
| GET | `/api/research/history?date=YYYY-MM-DD` | 本机原始真实批次、冻结清单及已知结果池的白名单导出 |
| GET | `/api/research/ai-dataset?id=实验ID` | 仅开发日期的机器数据，不含保留日期标签 |
| POST | `/api/research/proposals` | 归档外部模型提出的七因子权重，不自动应用 |
| GET | `/api/research/proposals` | 读取最近最多 100 条待验证参数建议 |

原始历史接口按导入契约归一为安全字段，不调用远程；每日期最多 5,000 批，09:10—09:26 拒绝此类重导出。缺结果池时 `outcome=null`，此时导出只能审计，不能直接作为满足完整结果要求的导入文件。原始留存不受单次研究选用最近 120 日的范围限制。

开发集不足时 AI 数据的 `sessions` 可以为空。即使尚未满足搜索门槛，该出口也预留最后至少 5 日，不为了凑模型输入泄露保留标签。**每日完整 JSON、完整实验及原始历史导出可能包含保留日期结果，不可把这些结果交给自动调参模型。** 优先使用 `ai-dataset`。人或模型一旦依据保留结果继续调参，就要使用新的日期检验。

参数建议格式为 `{experiment_id, weights, source_model, rationale}`：`weights` 必须包含七个因子的有限非负权重、总和大于零；`source_model` 1—100 字，`rationale` 最多 4,000 字。返回 `awaiting_future_validation`，保存建议及证据关联；这个入口只归档，不自动将外部建议加入本轮搜索，也不切换实盘策略。需要开发者安排新的未使用日期验证，再由用户决定采用。

下面 PowerShell 示例只访问已启动的本机服务，不调用外部金融或模型接口，不修改权重，不包含密钥。运行实验后等待页面任务完成，再读取结果。

```powershell
$auctionBase = 'http://127.0.0.1:8765'
$auctionHeaders = @{ 'X-Local-App' = 'auction-lab' }
Invoke-RestMethod -Method Post -Uri "$auctionBase/api/research/run" -Headers $auctionHeaders -ContentType 'application/json' -Body '{}'

# 等待页面显示实验完成后执行：
$auctionResearch = Invoke-RestMethod -Uri "$auctionBase/api/research"
$auctionExperiment = $auctionResearch.id
if (-not $auctionExperiment) { throw '尚无已完成实验，请先检查研究任务状态。' }
$auctionDevelopment = Invoke-RestMethod -Uri "$auctionBase/api/research/ai-dataset?id=$auctionExperiment"
$auctionDevelopment | ConvertTo-Json -Depth 30

# 把真正由开发集研究得到的建议保存为当前项目目录的 proposal.json 后，可显式归档：
# $auctionProposalPath = Join-Path (Get-Location) 'proposal.json'
# $auctionProposal = Get-Content -LiteralPath $auctionProposalPath -Raw -Encoding UTF8
# Invoke-RestMethod -Method Post -Uri "$auctionBase/api/research/proposals" -Headers $auctionHeaders -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($auctionProposal))
```

所有 POST 继续接受本机 Host、同源及 `X-Local-App: auction-lab` 检查。模型应调用专用 API，不读取凭据或扫描其他任务。参数建议文案同样不能填入模型密钥。

当前建议接口只归档 `awaiting_future_validation`，没有验证、批准或应用端点；内置优化器也不会自动读取外部提案。每日自动滚动实验可能重复已暴露留出日期，此时明确标为探索性结果。怎样登记未来新样本和由用户决定应用，见 [持续算法优化手册](CONTINUOUS_OPTIMIZATION.md)，不能把归档成功写成算法已经验证或启用。

## 留存与复现

| 路径 | 内容 |
| --- | --- |
| `data/market.sqlite3` 的 `batches` | 原始逐批接收证据，实盘与演示隔离；新批次含原权重 |
| 同库 `research_manifests` | 每日首次准备时冻结的清单、日历、上下文及权重 |
| 同库 `research_decisions` | 两个截止时点的原引擎排名、权重及源码证据；不将截止后批次混入 |
| `data/research/daily/YYYY-MM-DD-auction.json` | 每日只写一次的竞价冻结档案 |
| `data/research/daily/YYYY-MM-DD-auction.md` | 同一冻结对象的排名 Markdown 汇总，09:27 自动写出，只写一次 |
| `data/research/daily/YYYY-MM-DD-morning-ranking.md` | 09:26 写出的实时排名视图：可刷新、非不可变证据，不参与优化样本 |
| `data/research/daily/YYYY-MM-DD-校正ID.json/.md` | 归一化或实现缺陷修复后按当日原批次重建的校正版本，保留原冻结身份 |
| `data/research/daily/YYYY-MM-DD-核验ID.json/.md` | 对应结果证据的逐日核验版本 |
| `data/research/experiments/实验ID.json/.md` | 当前算法回放与因子实验，含数据、源码摘要和参数 |
| `data/research/imports.json` | 与原始实盘库隔离的历史导入及来源声明 |
| `data/research/holdouts.json` | 已用于独立检验的日期台账，不应删除后重用 |
| `data/research/proposals/建议ID.json` | 外部参数建议，仅归档，等待未来验证 |
| `data/research/latest.json` | 最近实验指针，不是历史原始数据 |

程序不因调参主动删除原始批次；当前单次研究有最近 120 日与每日期 5,000 批的计算上限，超限明确排除，不代表旧记录被删除。请自行备份数据目录；磁盘、人工删除和硬件故障不由应用保证恢复。源码摘要标识当时实现，不等于保存整个历史源码，严格复现还应保留对应 Git 版本。

测试验证时序、隔离、缺失、参数划分与归档机制；它不证明账户授权、真实十分钟采集成功或策略经济效用。具体本机验证状态另见 [VALIDATION.md](VALIDATION.md)，不覆盖此前验收记录。
