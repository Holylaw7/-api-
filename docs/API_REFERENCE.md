# 本机接口与扩展接口说明

适用版本：主系统 1.7.0，独立 AI 助手 1.2.0。本文是给用户、自动化脚本和其他 AI 的接口索引。字段的严格业务契约以根目录 [CONTRACT](../CONTRACT.md) 为准；每日优化工作流见 [CONTINUOUS_OPTIMIZATION](CONTINUOUS_OPTIMIZATION.md)。

## 1. 连接规则

| 服务 | 默认地址 | 用途 |
| --- | --- | --- |
| 主系统 | `http://127.0.0.1:8765` | 行情、竞价、个股、复盘、研究、报告、主页面 AI |
| 独立 AI 助手 | `http://127.0.0.1:8766` | DeepSeek / ChatGPT / 自定义模型聊天与显式盘面摘要 |

两个服务都只接受 `Host` 为当前端口的 `127.0.0.1` 或 `localhost`；提供 `Origin` 时必须同源。主系统所有 POST 需要：

```http
Content-Type: application/json
X-Local-App: auction-lab
```

独立助手 POST 将最后一项改为 `X-Local-App: auction-ai`。该头只是本机跨站防护标记，不是远程认证令牌。服务没有跨站 CORS，也不适合作为公网多用户 API。

主系统普通 POST 请求体上限 64 KiB，`/api/research/import` 上限 5 MiB；独立助手 POST 上限 64 KiB。不要把金融或模型 Key 放进 URL、查询参数、日志或调参数据。

同步成功通常返回 `{"ok":true,"message":"..."}`。后台任务另返回 `started`；`started=true` 只表示任务已创建，不表示完成。应通过 `/api/state`、`/api/events` 或对应 GET 结果检查：

```json
{
  "jobs": {
    "research": {"status": "running", "message": "..."}
  }
}
```

任务状态为 `running | done | error`。主系统 400 表示输入或当前状态不允许，403 表示本机 Host/Origin/操作头检查失败，404 表示路径或档案不存在，413 表示请求过大，500 表示受控的本机文件或内部失败。错误正文不应含 Key 或上游原始认证内容。

所有业务日期为 `YYYY-MM-DD`，时间按上海 UTC+08。百分数字段若名称带 `_pct`，通常是百分数原值或百分点；金额按元。证券代码使用 `000001.SZ`、`600519.SH`、`430047.BJ` 这类完整字符串并保留前导零。

## 2. 主系统读取接口

### 2.1 运行状态和本机观察

| 方法与路径 | 参数 | 返回 / 注释 | 联网或写入 |
| --- | --- | --- | --- |
| GET `/api/health` | 无 | `{ok,application:'auction-lab',version}` | 无 |
| GET `/api/state` | 无 | 页面公开快照；含模式、时钟、竞价、复盘、个股、AI、金融接入、任务和非敏感配置；不含 Key 与 `raw` | 无 |
| GET `/api/events` | 无 | SSE；`event: state`、`id: version`，变更即时推送，空闲 keepalive | 无 |
| GET `/api/diagnostics` | 无 | 本机准备状态和质量检查；不是远程认证检查 | 无 |
| GET `/api/history?symbol=完整代码` | `symbol`；裸六位只在本机唯一时接受 | `{symbol,date,mode,items}`，当前会话最多最近 1000 条原始观察 | 只读本机 SQLite |

`/api/state` 中和持续优化最相关的摘要为：

```text
mode: 'live'|'demo'
running: boolean
config.weights: 七因子当前权重
auction: {date,phase,summary,rows}
review: 当前公开复盘或 null
review_id: 当前复盘证据 SHA-256 或 null
research: {id?,status,generated_at?}
jobs: {prepare?,review?,daily_validation?,research?,...}
finance: {configured,persisted,credential_source,test,can_save,can_test,...}
```

SSE 只广播公开摘要。完整研究实验、每日档案、报告 `raw` 和板块明细须走专用 GET。

### 2.2 报告接口

| 方法与路径 | 参数 | 返回 / 注释 | 联网或写入 |
| --- | --- | --- | --- |
| GET `/api/report` | `date?`、`format=json|markdown`；Markdown 可带 `include_ai=0|1`、`provider?`、`review_id?`、`baseline?` | 当前/指定报告；JSON 下载保留证据，Markdown 为人读版本 | 只读本机 |
| GET `/api/reports` | 无 | `{mode,items,limit}`，当前模式最多 365 个报告日期 | 只读本机 |
| GET `/api/reports/compare?date=...&baseline=...` | 两个有效日期，baseline 更早 | 两期完整性核验后比较共同涨停、出现/退出和板块变化 | 只读本机 |
| GET `/api/reports/download?filename=...&mode=live|demo&sha256=...` | 使用保存接口返回的文件名和校验值 | 下载同一版本 Markdown；文件被替换后旧 SHA 拒绝 | 只读本机 |

缺池不是空池；非相邻日期的集合差不能自动称为晋级、首板或断板。报告重新生成会产生新 `review_id`，旧 AI 附录和旧板块证据不能直接附到新版本。

### 2.3 指定板块证据

| 方法与路径 | 参数 | 返回 / 注释 | 联网或写入 |
| --- | --- | --- | --- |
| GET `/api/sectors/research?date=...&review_id=...` | 已有真实报告日期和当前版本 | 对应该报告版本的最近板块证据或 `not_run` | 只读本机 |
| GET `/api/sectors/export?evidence_id=<24hex>` | 证据编号 | 完整白名单 JSON 下载 | 只读本机 |

GET 不会为了补齐证据访问金融接口。实际取数由下方 POST 明确启动。

### 2.4 每日研究和算法优化

| 方法与路径 | 参数 | 返回 / 注释 | 联网或写入 |
| --- | --- | --- | --- |
| GET `/api/research` | 无 | 最近实验对象；没有实验时 `{status:'not_run'}` | 只读本机 |
| GET `/api/research/template` | 无 | `schema_version=1` 的空历史竞价导入模板 | 无 |
| GET `/api/research/export?id=<24hex>&format=markdown|json` | `id` 可省略取最近 | 不可变完整实验下载；可能包含保留期结果 | 只读本机 |
| GET `/api/research/ai-dataset?id=<24hex>` | `id` 可省略取最近 | 专供调参模型的 `development_only` 数据；隐藏保留期明细 | 只读本机 |
| GET `/api/research/history?date=YYYY-MM-DD` | 指定真实本机日期 | 盘前清单、全日批次白名单和已知结果池；用于审计/迁移，不应用于反复选参 | 只读本机；保护时段拒绝 |
| GET `/api/research/proposals` | 无 | `{items,limit:100,automatic_application:false}` | 只读本机 |
| GET `/api/research/daily` | 无 | `{items}`，最多 365 个每日冻结/校正/核验摘要 | 只读本机 |
| GET `/api/research/daily?date=YYYY-MM-DD` | 日期 | 最近核验版本；没有则校正版本，再否则竞价冻结版或 `unavailable` | 只读本机 |
| GET `/api/research/daily/export?date=...&format=markdown|json` | 日期、格式 | 所选版本的每日不可变档案下载；`markdown` 是同一对象的排名汇总 | 只读本机 |

每日 `markdown` 不需要先手动保存：09:25 后约一分钟先写实时排名视图 `data/research/daily/YYYY-MM-DD-morning-ranking.md`（09:26 分钟内按新批次刷新，声明为非不可变证据），09:27 冻结时自动写出 `YYYY-MM-DD-auction.md`，15:10 后的核验版本另写 `YYYY-MM-DD-<24位ID>.md`；冻结件与核验件只写一次且不改写冻结分数。整日因归一化或实现缺陷不可评分时，维护者用 `python tools/rebuild_daily_ranking.py --date YYYY-MM-DD --reason "..."` 生成独立校正版本（只读本机 SQLite，不联网），再由 15:10 核验把标签加到重建分数上。

`/api/research/ai-dataset` 的稳定顶层结构：

```json
{
  "schema_version": 1,
  "experiment_id": "24位十六进制ID",
  "scope": "development_only",
  "dataset_sha256": "64位SHA-256",
  "baseline_weights": {
    "gap": 0.15,
    "amount": 0.15,
    "turnover": 0.1,
    "volume_ratio": 0.1,
    "late_momentum": 0.2,
    "retention": 0.15,
    "continuity": 0.15
  },
  "definition": "...",
  "sessions": [],
  "excluded_date_count": 5,
  "proposal_endpoint": "POST /api/research/proposals",
  "rules": []
}
```

每个 `session` 对应 09:24:50 的真实合格日期，核心字段为：

```text
date, previous_date, mode, origin, checkpoint, prepared_at, point_in_time,
status, weights, rows, quality, warnings, definition
```

每个 `row` 至少包含 `thscode,name,score,raw_score,rank,label,factors,quality`。`label` 仅表示该股票是否属于同日 15:10 后取得的完整涨停池；不是收益或可成交标签。`factors` 的七个键各含 `value,score,weight,available,contribution`。只有 `quality.eligible_for_optimization=true` 的 09:24:50 会话才会进入开发出口。

完整实验的 `optimization` 重点字段：

```text
status
sample_summary.{complete_days,complete_rows,positive_count,negative_count,excluded,missing_factors}
baseline
search.{objective,development_dates,holdout_dates,folds,selected_candidate}
candidate_weights
recommendation.{accepted,automatic_application,reason,...}
holdout
factor_diagnostics
warnings
```

完整实验和 `/api/research/history` 可能含保留日期结果。给自动选参模型时只使用 `ai-dataset`。

## 3. 主系统写入和任务接口

### 3.1 运行控制

| 方法与路径 | JSON 请求 | 行为与前置条件 |
| --- | --- | --- |
| POST `/api/start` | `{}` | 验证金融 Key 后切到 live 并启动调度；可能联网准备/采集 |
| POST `/api/stop` | `{}` | 停止自动采集，保留数据和本机服务 |
| POST `/api/prepare` | `{}` | 后台读取交易日历、上一交易日完整涨停池及所需证券目录，冻结盘前清单 |
| POST `/api/demo` | `{}` | 启动明确标记的合成演示；不作实盘研究证据 |
| POST `/api/demo/exit` | `{}` | 取消演示并恢复停止的 live 视图；不联网、不启动 |
| POST `/api/review` | `{"date":"YYYY-MM-DD"}`，日期可省略 | 后台生成已收盘交易日复盘；09:10—09:26拒绝；同日 15:10 后可触发每日标签与研究 |

`prepare`、`start`、`review` 会涉及官方数据；`stop` 和 `demo/exit` 不联网。准备晚于 09:15 的清单只作回溯说明，不进入严格优化。

### 3.2 金融接入

| 方法与路径 | JSON 请求 | 行为与返回 |
| --- | --- | --- |
| POST `/api/finance/config` | `{"api_key":"用户自己的Key"}` | 只保存到项目外用户凭据文件，不测试、不启动；返回无密钥 `finance` 状态 |
| POST `/api/finance/test` | `{}` | 后台单次官方交易日历验证，`max_retries=0`；不启动监测 |
| POST `/api/credentials` | `{"api_key":"..."}` | 旧客户端兼容：保存后立即启动；新集成不要使用 |

读取状态使用 GET `/api/finance/status`。`configured=true` 只表示本机存在 Key；只有 `test.status='success' && test.ok=true` 表示本进程最近一次日历验证通过。验证不证明全部接口权限。

### 3.3 配置、股票与趋势池

| 方法与路径 | JSON 请求 | 行为与前置条件 |
| --- | --- | --- |
| POST `/api/config` | 非敏感配置的部分对象；`weights` 必须一次包含七键 | 保存配置并重置当前竞价会话内存；要求停止采集且无任务运行 |
| POST `/api/watchlist/add` | `{"codes":["000001","600519.SH"]}` 或分隔字符串 | 后台官方精确解析；新关注下一轮生效，不补造此前数据 |
| POST `/api/watchlist/remove` | `{"code":"000001.SZ"}` | 只移除 manual 来源；自动重点来源仍可保留 |
| POST `/api/stocks/analyze` | `{"code":"600519.SH","date":"YYYY-MM-DD"}` | 后台一次单股前复权日线分析；不自动加自选 |
| POST `/api/trends/refresh` | `{}` | 后台刷新有限候选趋势池；保护时段拒绝 |

通过 `/api/config` 写权重不会关联某个 AI 提案，也不会把该提案标记为已采纳。当前没有自动应用、采纳或拒绝提案的端点；权重变更必须作为用户批准的单独维护操作，并保留提案、实验和 Git 记录。

### 3.4 报告和板块任务

| 方法与路径 | JSON 请求 | 行为与返回 |
| --- | --- | --- |
| POST `/api/reports/load` | `{"date":"YYYY-MM-DD"}` | 读取本机历史视图，不请求行情，不改变最新自动报告 |
| POST `/api/reports/save` | `{date?,include_ai?,provider?,review_id?,baseline?}` | 保存 Markdown，返回 `filename,mode,review_id,sha256,download_url` |
| POST `/api/reports/enrich` | `{"date":"...","review_id":"64hex"}` | 后台重试/刷新风向标与三类龙虎榜（生成复盘时已自动读取一次）；生成新报告版本 |
| POST `/api/sectors/search` | `{"query":"PCB"}` | 查询官方行业/概念目录；缓存未命中时会联网 |
| POST `/api/sectors/research` | `{"codes":["885959.TI"],"date":"...","review_id":"64hex"}` | 后台有界取数并独立保存证据，不调用模型 |

板块代码必须来自本次官方目录，最多 3 个。当前成员不能当作历史成分；成交额不能当作净资金流。

### 3.5 研究、导入与外部 AI 建议

| 方法与路径 | JSON 请求 | 行为与返回 |
| --- | --- | --- |
| POST `/api/research/run` | `{}` | 后台本机回放、固定共同样本和时间检验；不调用金融或模型接口 |
| POST `/api/research/import` | `{"dataset":{...schema_version:1...}}` | 保存明确来源的历史序列；不写 live SQLite，不覆盖已有日期 |
| POST `/api/research/proposals` | 见下例 | 只归档权重建议，返回 `proposal_id,status,automatically_applied:false` |

提案请求：

```json
{
  "experiment_id": "0123456789abcdef01234567",
  "weights": {
    "gap": 0.15,
    "amount": 0.15,
    "turnover": 0.10,
    "volume_ratio": 0.10,
    "late_momentum": 0.20,
    "retention": 0.15,
    "continuity": 0.15
  },
  "source_model": "模型和版本",
  "rationale": "只引用开发集证据、缺失和风险；最多4000字"
}
```

七个值必须为有限非负数，总和大于 0；程序会归一化。该接口不能提交新因子公式、代码补丁或任意字段，也不会让提案进入当前优化器。当前提案状态固定为 `awaiting_future_validation`，未来验证和采纳仍需明确维护流程，详见持续优化文档。

### 3.6 主页面 AI

| 方法与路径 | JSON 请求 | 行为与返回 |
| --- | --- | --- |
| POST `/api/llm-config` | `{provider,api_key?,model?,base_url?}` | 分服务商保存配置；Key 不回显 |
| POST `/api/llm-select` | `{"provider":"deepseek|openai|custom"}` | 切换后续请求使用的服务商 |
| POST `/api/llm-test` | `{"provider":"..."}` | 后台读取模型列表检查；不生成付费回答 |
| POST `/api/llm` | `{question?,provider?,review_date?,review_id?,fetch_sectors?:false,sector_codes?}` | 后台生成白名单摘要；结构化竞价排名不等待模型 |

提供 `review_date` 时生成指定报告专属回答；其 `review_id` 必须仍匹配。指定 `sector_codes` 时必须 `fetch_sectors=true`。模型失败不会自动切换另一服务商，也不会重试付费 POST。

## 4. 独立 AI 助手接口（8766）

| 方法与路径 | 请求 | 返回 / 行为 |
| --- | --- | --- |
| GET `/api/health` | 无 | `{ok,application:'auction-ai-assistant',version:'1.2.0'}` |
| GET `/api/state` | 无 | `{ai,messages,job,connection_test,context}`，无 Key |
| POST `/api/profiles/save` | `{provider,api_key?,model?,base_url?}` | 保存该服务商；空 Key 保留已有值 |
| POST `/api/profiles/select` | `{"provider":"..."}` | 切换服务，对话仍按服务商隔离 |
| POST `/api/profiles/test` | `{"provider":"..."}` | 后台检查模型列表，不生成回答 |
| POST `/api/context/load` | `{}` | 从固定本机主系统或缓存报告加载摘要预览 |
| POST `/api/chat` | `{message,include_market:false,provider?}` | `include_market=true` 前必须先加载预览；最多 8000 字 |
| POST `/api/chat/clear` | `{provider?}` | 清除该服务商本机对话；忙时拒绝 |

独立助手不会启动主系统 `Service`，也不会隐式读取同花顺接口。普通聊天默认不附市场资料；加载摘要不等于发送，必须由用户勾选后才随消息传给模型。

## 5. Python 内部扩展接口

外部脚本优先使用本机 HTTP。维护源码或添加数据商适配时，以下内部接口是稳定责任边界，不应绕过 service 的时段、模式和存储保护。

### 5.1 官方数据适配器

`HiThinkProvider(api_key,min_interval=.5,timeout=6,max_retries=3)` 只向固定官方 HTTPS 地址发送 `X-api-key`。常用方法：

| 方法 | 返回与限制 |
| --- | --- |
| `calendar()` | 排序去重的 ISO 交易日列表 |
| `pool(kind,date)` | `limit-up / limit-down / limit-break` 全分页池 |
| `auction(codes,stage='live')` | 最多 100 个代码；单批竞价层禁网络重试 |
| `resolve_stock(code)` | 官方精确 A 股解析，不猜后缀 |
| `stock_quote(codes)` | 显式代码当前快照 |
| `market()` | 全分页市场表现和各页时间戳 |
| `catalog(tag)` | 行业或概念目录 |
| `indices(codes)` / `members(code)` | 指数快照 / 当前成员 |
| `ladder()` | 官方连板天梯样本，不代替完整涨停池 |
| `tickers(asset_type='a-share')` | 全分页证券目录 |
| `historical(code,start,end,index=False,adjust='none')` | 股票/指数历史；趋势研究显式前复权 |
| `index_historical(code,start,end)` | 指数日线，不带股票复权参数 |

成功必须同时满足 HTTP 200 和上游 `code==0`。`APIError` 只保留安全错误码、请求 ID 和限流秒数；不要把上游正文送到页面或模型。

### 5.2 评分与研究接口

| 接口 | 契约 |
| --- | --- |
| `AuctionEngine(weights=None)` | 七键非负权重，初始化时归一化 |
| `engine.ingest(data,received_at,context)` | 每批一次；`received_at` 必须带时区；context 只能是前日证据 |
| `engine.rankings(now=None)` | 返回分数、因子、质量、覆盖和稳定排序 |
| `replay_session(manifest,batches,outcome,weights,checkpoint=...)` | 只摄入截止前记录；outcome 只生成 label |
| `optimize_sessions(sessions,baseline_weights,top_k=10)` | 纯计算；固定共同七因子样本，按日期切分，不修改输入 |
| `DailyValidation.freeze(date,now)` | 09:27 后建立不可变竞价档案 |
| `DailyValidation.label(report,now)` | 15:10 后生成独立收盘核验版本 |
| `ResearchLibrary.run(weights,now,...)` | 读取本机/导入证据、回放、实验并按内容 ID 归档 |
| `ResearchLibrary.ai_dataset(id)` | 只导出开发日期；供外部模型使用 |
| `ResearchLibrary.save_proposal(body,now)` | 归档七权重建议，不应用 |

## 6. 本机数据位置

| 路径 | 内容 | 可否直接给调参 AI |
| --- | --- | --- |
| `data/market.sqlite3` | live/demo 批次、报告、盘前清单、时点决策 | 否；优先走白名单接口 |
| `data/research/daily/` | 每日竞价冻结、排名 Markdown（含 09:26 实时视图）、校正版本与收盘核验 | 只作审计；实时视图非证据，可能含后来标签 |
| `data/research/experiments/` | 完整实验 JSON/Markdown | 否；含保留期结果 |
| `data/research/holdouts.json` | 已暴露保留日期台账 | 只读治理证据，不删除 |
| `data/research/proposals/` | 外部 AI 待验证建议 | 可用于追溯，不能视为已通过 |
| `data/research/imports.json` | 与 live 隔离的外部历史声明 | 只在来源和时点经用户核实后使用 |
| `data/reports/` | 收盘报告与 Markdown | 用于盘面解释，不替代竞价轨迹 |

项目源码包不包含上述 `data/`。其他 AI 不应扫描凭据目录或直接改 SQLite；程序化研究先通过 HTTP 取得最小必要白名单。

## 7. 最小调用示例

以下 PowerShell 示例只访问本机，不携带 Key：

```powershell
$auctionBase = 'http://127.0.0.1:8765'
$auctionHeaders = @{ 'X-Local-App' = 'auction-lab' }

# 查看状态
$state = Invoke-RestMethod "$auctionBase/api/state"

# 运行本地历史实验
Invoke-RestMethod -Method Post -Uri "$auctionBase/api/research/run" `
  -Headers $auctionHeaders -ContentType 'application/json' -Body '{}'

# jobs.research 变为 done 后，取得仅开发集数据
$experiment = Invoke-RestMethod "$auctionBase/api/research"
$development = Invoke-RestMethod "$auctionBase/api/research/ai-dataset?id=$($experiment.id)"
```

如果 `development.sessions` 为空，表示可用开发样本不足。模型应返回“暂不建议改权重”，不能根据完整报告、演示数据或收盘池自行补造训练集。
