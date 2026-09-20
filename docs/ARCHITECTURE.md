# 架构与二次开发

版本1.7。运行与接口以 `app/service.py`、`app/server.py` 为准；公式见策略文档，字段契约见根目录 `CONTRACT.md`。同花顺接入见 `docs/FINANCE_CONNECTION.md`，指定板块取数见 `docs/SECTOR_RESEARCH.md`，历史回放和每日核验见 `docs/BACKTEST.md`，1.4 官方观察流程见 `docs/OFFICIAL_UPGRADE.md`。

## 模块与数据流

```text
同花顺 REST / 用户提供的模型 API
       ↓                 ↑ 点击才调用
provider.py           llm.py
       ↓                 ↑ 结构化摘要
service.py → engine.py → 内存最新排名 → SSE → 本机浏览器
       ↓
storage.py → SQLite / JSON / Markdown 报告
       ↑
review.py → 全池复盘、连板趋势、板块成交参与度
stocks.py → 单股日线研究 → stocks/个股日期.json
selection.py → 有限候选趋势筛选 → trend-pool.json → 下一会话重点池
report_library.py ↔ storage.py / 报告JSON → reporting.py → Markdown保存与下载
insights.py → 两期报告比较 / 本机就绪检查（不请求行情）
sentiment.py → 留存完整池的情绪结构（不请求行情）
official_context.py → 手动、显式日期的风向标 / 龙虎榜 → 新版本报告
sector_research.py → 指定板块与成员取数 → sector_library.py → 独立证据档案
                                                ↓
                            llm.py targeted_sectors → 一次可选模型生成
daily_validation.py → 当日两时点评分冻结 → 收盘结果核验 → 每日版本档案
research_data.py → 有界历史JSON导入 → research.py → replay.py → AuctionEngine
                                              ↓
                                      optimization.py → 参数建议 / 实验JSON与Markdown
```

全项目 Python 标准库，前端原生 HTML/CSS/JS，无第三方 CDN。Windows 使用固定 UTC+08:00（Asia/Shanghai 现代交易时区），不依赖系统时区数据库。所有入库时间包含偏移。

## 调度状态

日历来自官方近一年交易日历，不能以“周一到周五”代替。每天重新读取日历和上一交易日涨停池。目录准备失败则显示错误并每分钟有界再准备；日历未知不当成已知交易日采集。

| 时段 | 行为 |
|---|---|
| 建议09:05前启动 | 准备昨日池、趋势池与证券目录；09:10前完成准备；恢复同日真实批次 |
| 09:10–09:26（含整分钟） | 竞价优先；个股日线延后，拒绝新复盘/手动趋势刷新，进行中的趋势筛选在下次取数前停止 |
| 09:15:00–09:19:59 | `stage=live`，每批落盘、增量评分、推送 |
| 09:20:00–09:24:59 | 继续 `live`，计算后段斜率与金额留存 |
| 09:25:00–09:26:00 | 逐批 `final`；只请求未完成的证券 |
| 09:26 后 | 停止竞价请求；保留终态或明确待核验的最后观察 |
| 09:27起 | 执行待处理个股日线；自动重试被保护时段打断的趋势筛选 |
| 15:10 后 | 自动生成当日复盘；完成后触发趋势池再筛选，利用当天活跃股准备下次会话 |

当请求跨越 09:25 返回，保留真实接收时间，不倒填成 09:24:59。实时端点每批不在网络层重试，下一轮按调度重试；一般 REST 最多重试 3 次。限流和无效密钥不会立即重复轰炸接口。复盘在独立线程进行，09:10–09:26 禁止开始新的复盘任务。系统不宣称操作系统调度或网络具有硬实时保证。

关注池构建与观察引擎分离。`_rebuild_codes()` 在锁内根据 `manual`、`previous_limit_up`、`strong_trend` 合并，`all` 模式再加 `all_market`。增删自选只改成员和来源，不调用引擎reset；在途一轮使用已拷贝的代码列表，新加入者从下一轮开始。只有切换真实会话/演示或重建新会话才按恢复流程处理引擎。移除后若仍有自动来源则继续跟踪；旧观察保留。

趋势池按截止日匹配上一交易日。`session_trends` 保留本次会话使用的旧池，收盘后新生成的下一次池不会反向改写本次竞价来源。纯自选模式不自动合入涨停或趋势池。趋势筛选以 `should_stop` 检查关闭和保护时段，中断后清除本次自动尝试标记以便稍后重试；同日已完成池不会被中断的partial结果覆盖。

个股查询先精确解析代码。只含完整代码的缓存条目不能据此证明六位裸代码唯一；裸代码复用须具备该裸代码的官方解析凭据，否则重新查询官方搜索。`stocks.py` 只请求一只股截至已收盘日的前复权历史，不触发全市场、竞价或模型调用。保护时段将单个待处理查询存在 `pending_stock`，新延后查询覆盖上一次；SSE仍展示本机已有观察。

## 增量计算契约

`AuctionEngine.ingest(data, received_at, context)` 每收到一批调用一次。context 仅允许交易日前的上下文；同日/未来数据拒绝，避免盘前使用收盘后的已知涨停结果。上游 `null` 不补零；未匹配量因单位/方向未明确，不推断买压。

每条分数包含原值、子分数、权重、贡献、数据覆盖和质量说明。有重复请求、未就绪、迟到、缺值时保留诊断。分数和因子可供另一个 AI 使用，但必须一并传递质量说明。严格连板数不能从 `5天4板` 的计数直接推成 4 连板。

09:25 之后 root service 只允许临近截止 30 秒内的既有有效分数以 `is_provisional=true` 展示；终态未确认会显示质量标记，次日不沿用。原始引擎保持过期排除规则。

## 本机 HTTP 接口

仅监听 `127.0.0.1:8765`。浏览器界面无外部服务端，接口不提供跨站 CORS。

| 方法与路径 | 说明 |
|---|---|
| GET `/api/health` | 本服务识别和版本 |
| GET `/api/finance/status` | 本机同花顺配置/验证状态，无密钥，不联网 |
| POST `/api/finance/config` | `{api_key}`，只保存用户级Key，不启动或测试 |
| POST `/api/finance/test` | `{}`，后台一次官方日历认证测试，结果经finance及jobs推送 |
| POST `/api/demo/exit` | 退出演示、恢复本机live视图，不采集 |
| GET `/api/state` | 全部页面所需公开状态，不含密钥 |
| GET `/api/events` | `event: state` 的 Server-Sent Events，断线可重连 |
| GET `/api/history?symbol=完整代码` | 当前会话已采集该股原始观察 |
| GET `/api/report` | 当前或指定date的复盘，format=json/markdown；Markdown可选同版本AI附录与baseline对比 |
| GET `/api/reports` | 当前模式的本机历史目录，最多365个日期 |
| GET `/api/reports/compare` | date/baseline两期本机报告比较，不重新采集 |
| GET `/api/reports/download` | filename/mode/sha256校验后的已保存Markdown下载 |
| GET `/api/diagnostics` | 已知本机状态的就绪检查，不远程验证授权 |
| POST `/api/start` `/api/stop` | 启动/停止自动采集 |
| POST `/api/prepare` | 后台准备股票池 |
| POST `/api/review` | `{"date":"YYYY-MM-DD"}`，日期可省略 |
| POST `/api/reports/load` | `{"date":"YYYY-MM-DD"}`，读取实盘历史视图 |
| POST `/api/reports/save` | `{date?,include_ai?,provider?,review_id?,baseline?}`，原子保存并返回带SHA-256的下载地址 |
| POST `/api/reports/enrich` | `{date,review_id}`，后台读取该日风向标与三类龙虎榜；需要已有真实报告及原始日历，保护时段禁用 |
| POST `/api/demo` | 显式进入合成演示 |
| POST `/api/config` | 合并并校验非敏感配置 |
| POST `/api/watchlist/add` | `{"codes":["000001","600519.SH"]}`；也支持分隔文本；1–50只，保护时段每次1只；异步官方核验 |
| POST `/api/watchlist/remove` | `{"code":"000001.SZ"}`；移除manual来源并保留观察；裸代码必须在自选中唯一 |
| POST `/api/stocks/analyze` | `{"code":"000001","date":"YYYY-MM-DD"}`；date可省略；异步独立查询，不自动加自选 |
| POST `/api/trends/refresh` | `{}`；按最近已收盘日后台刷新有限候选趋势池，保护时段拒绝开始 |
| POST `/api/credentials` | 保存数据 Key 到用户凭据文件并启动 |
| POST `/api/llm-config` | 保存模型端点、模型 ID、独立 Key |
| POST `/api/llm` | question/provider可选；review_date/review_id限定为指定版本报告专属研判；fetch_sectors=true及sector_codes开启先取指定板块再调用模型 |
| POST `/api/sectors/search` | query名称或完整代码；读取/复用行业概念全目录，返回候选 |
| POST `/api/sectors/research` | codes/date/review_id；后台只取数，不调用模型 |
| GET `/api/sectors/research` | date/review_id；本机读取对应版本最近证据 |
| GET `/api/sectors/export` | evidence_id；校验并下载独立证据JSON |

所有 POST 要求同源及 `X-Local-App: auction-lab`；请求 Host 必须是本机服务地址，降低跨站和 DNS 重绑定风险。GET 不回显凭据。静态文件是白名单，不能下载 SQLite、Python 源码或凭据。远程 LLM 强制 HTTPS，重定向禁止转发 Key。

1.7的网页接入流程为保存→显式测试→按需启动。`finance`是独立状态对象，configured/persisted不表示验证成功；测试仅在当前进程保存，无自动外网健康探针。`finance_test`后台任务使用独立provider、固定官方地址、单次日历业务请求且max_retries=0；不进入采集批次路径。凭据读取顺序为用户文件、进程环境、Windows用户环境；网页更新后旧环境值不覆盖新文件。新Key重置provider、目录缓存、验证状态；保存要求无运行监测或活动任务，演示/竞价保护时段拒绝保存和测试。

默认启动行为仍兼容已有用户自动监测；`run.py --no-auto-start`传递`serve(auto_start=False)`仅启动本机服务。启动器识别旧版本后台并明确要求重启，不终止未知或旧进程。项目路径取自源码位置，用户级凭据在项目外，源码包可部署到任意可写目录。

复盘、补充官方观察、添加自选、个股分析、趋势刷新、AI生成和AI连接测试响应含 `{ok,message,started}`。`started=false` 表示同名任务已在运行，并非又启动一个任务。`jobs` 的键包括 `prepare/review/evidence/llm/llm_test/stock/watchlist/trends`，每个值为 `{status:'running'|'done'|'error',message}`。保护时段个股任务可能已经 `done`，但 `stocks.analysis.status='deferred'`，表示日线在等待09:27，不应当显示计算成功或零分。

新增公开状态为：

```text
stocks = {
  query_code,
  watchlist: [{thscode,name,sources,tracking}],
  analysis: null | {
    thscode,name,date,status,trend,trend_score,trend_factors,trend_coverage,
    history,warnings,source,score_method,
    auction:{row,history,date,mode,tracking,scope_count}
  },
  trend_pool:{date,status,rows,candidate_count,evaluated_count,
              valid_history_count,selected_count,coverage,warnings,...}
}
auction.source_counts = {manual,previous_limit_up,strong_trend}
auction.rows[].sources = ['manual', ...]
```

`analysis.date` 是日线截止日，`analysis.auction.date` 是本机竞价会话日。后者附加于公开快照，不通过实时HTTP补取历史竞价。`tracking` 说明是否属于当前正在采集范围；有旧观察但已移出范围时不再给当前范围名次。GET状态递归去除大体积 `raw`，完整复盘由导出与落盘文件保留。

## 持久化与恢复

SQLite 表 `batches` 保存 `session_date, mode, received_at, stage, payload`。mode 有 `live` 和 `demo`，查询必须显式按 mode 分离。表 `reports` 按日期与模式保存最新复盘。原始数据与 derived 分数独立，修改策略后可以用原始批次重建；重新评分与最初实时可见结果应区别记录。

复盘 JSON 保留 raw 字段作为证据，网页主要展示 derived 部分。写 JSON 使用临时文件再原子替换。用户凭据文件和模型配置位于项目外部；备份项目目录不自动备份 Key。

| 文件 | 内容与复用边界 |
| --- | --- |
| `data/stock-metadata.json` | 以完整代码为键保存官方代码、名称、资产类别及 `resolution`；缓存不能绕过裸代码歧义校验 |
| `data/trend-pool.json` | 最近一次趋势筛选结果、截止日、候选覆盖、评分及警告；加载后须按会话上一交易日核对 |
| `data/stocks/代码-日期.json` | 独立单股日线结果，如 `600519.SH-2026-09-18.json`；前复权口径和来源完整保存 |
| `data/config.json` | 自选完整代码和非敏感设置；代码按字符串保存，不能丢失前导零 |

演示中拒绝真实股票查询、自选增删、真实准备和趋势刷新；公开状态不展示真实个股分析或趋势池为演示信号。回到真实模式后继续使用真实持久化数据。退出服务会丢失尚未执行的内存延后查询，需重新提交；已保存的日线结果和观察仍在本机。

`_complete_trend_cache(pool,date)` 区分“取数已完成”与“没有质量提示”。同日ready结果可复用；partial若已处理全部候选、至少5日池齐全、没有history失败或保护中断，并有completed_at，也可复用。短上市历史或日期推断警告仍保留，但不会导致每次重启重新下载60只日线。中断、实际请求失败和候选未完成不满足此条件；最新复盘完成后仍会显式要求再筛选。

## 扩展策略

改权重：页面直接编辑；修改生效前必须停止采集。默认七因子加总归一化。新增因子：修改 `engine.py` 的因子定义与归一化、`config.py` 校验、前端名称映射、策略文档及有意义的时序测试，不能只改 UI。

改数据源：实现 `HiThinkProvider` 对应方法，保留标准字段、时间、单位、分页完成语义。股票池返回失败不能转换为成功空列表。

改个股或自动筛选：分别修改 `stocks.py` 与 `selection.py` 的公式，不共用两个同名 `_trend_score` 的含义。同步公式说明、硬阈值、覆盖率单位和前端标签；保持单股40交易日/一次日线请求、筛选60候选/30入选上限与保护时段。`selection.py` 不写磁盘，缓存及会话合并由service负责。

接真实资金流：`review.py` 的 `CapitalFlowSource` 定义扩展协议，提供 `sector_flows(date, sector_codes)`；需返回数据源、交易日、币种、资金口径及原始时间。协议仅为扩展接口，当前未安装外部资金源、未自动调用、不承诺已有真实净流。接入后另建净资金排行，不替换“成交参与度”字段。概念板块成分可能重叠，不能跨板块加总金额。

改 LLM：`ai_gateway.py` 统一服务商配置、模型列表检查和文本生成；DeepSeek/自定义使用 Chat Completions，OpenAI 使用 Responses。`llm.py` 只构造递归白名单盘面摘要。只接收白名单市场摘要、在独立后台任务运行、错误不影响数值策略。不能把模型文本作为委托指令。

## 性能与验收

API 每批 100，串行请求优先保证限流安全。服务每批排名，SSE 刷新后浏览器读取，不等收齐所有批次。全市场上限由网络时延与官方频率限制决定，不能用 UI 的 3 秒配置冒充每只股票真实 3 秒更新。

SSE以service.version检查状态变更，变更后立即发完整状态，每批到达仍走原增量发布路径。无变更时condition最长等待3秒，发送轻量注释keepalive；普通闲时每15秒补完整状态，09:10–09:26保护时段每3秒补状态，更新时钟与陈旧提示。客户端重连建议2秒。完整状态刷新间隔与金融API轮询间隔不是同一概念。报告identity按发布对象缓存，避免每次SSE重算大报告指纹；不得通过原地修改报告破坏缓存假设。

## 本机报告库与版本绑定（1.3）

`Store.list_reports(mode,limit)`只查日期、模式、生成时间，最多365行，不解码每份大型raw。`ReportLibrary.list()`合并SQLite与相应模式目录的JSON日期；读取优先SQLite，缺记录才回退本机JSON，并验证日期和模式。历史读取、比较、保存、下载和就绪检查均不会重新调用金融数据服务。用户主动生成复盘仍走原后台采集与保护时段规则。

Service分开维护`review`、`latest_review`和`viewing_archive`。前者只代表用户当前看的报告，后两者让自动复盘与下一会话趋势准备跟随最新真实报告。载入旧报告不改latest_review；自动生成完成时只在未查看历史情况下切换当前视图；手动生成会切换。自动调度判断当日是否已完成使用latest_review，避免仅因看了旧报告就重复发起当日取数。

报告完成后先落SQLite与JSON、生成Markdown，再发布完整对象。**发布后的报告对象不可原地修改**；任何修订都创建新报告对象。`report_identity()`规范序列化公开证据并取SHA-256，递归排除raw、凭据敏感键、身份及导出/AI/对比元数据；保留生成和完成时间。`_review_identity()`按对象身份缓存哈希，公开状态新增`review_id`。保存或专属AI提交`review_id`后，版本不同则拒绝，不将相同日期的两次报告误认同一份。

`reporting.render_markdown`是纯函数；展示市场、涨停前30、板块前30、逐日梯队、日线趋势、覆盖与全部公开质量警告，并提供基于前10涨停强度对象的下一交易日观察清单。名单仅供后续核验，不自动加入关注。文本进行Markdown/HTML转义，模型附录作为引用文本，不允许模型内容变为可执行HTML。`compare_reports`同样纯计算，完整池核验后按全代码比较；非相邻交易日不声称昨日晋级或断板，日期推断和连板下限保留provisional。板块只说明相对涨幅、成交额变化与样本排名，不把它们命名为净流入。

保存使用同目录临时文件、UTF-8与LF、flush/fsync后os.replace。实盘Markdown路径为`data/reports/YYYY-MM-DD.md`，带AI为`YYYY-MM-DD-with-ai-provider.md`；演示使用`data/reports/demo/`。固定有效日期和服务商枚举组成文件名，下载端不接收路径；下载大小上限4MB，并可验证保存时返回的字节SHA-256。同名文件会被新保存原子替换，旧校验值下载失败可避免保存与下载之间取到不同版本；这不是无限版本归档。GET动态Markdown导出不写盘，POST保存返回path、filename、date、mode、review_id、sha256及download_url。

专属报告AI请求携带`review_date`，只将该报告的白名单摘要送入模型，移除当前竞价与个股数据；`scope='review'`结果绑定日期、模式、服务商、模型和review_id。`scope='market'`的综合盘面解释不得附入报告。专属结果按`data/ai-reviews/{mode}/YYYY-MM-DD-{provider}.json`保存安全白名单，可重启恢复；所有绑定字段匹配才能显示或附录。在途结果遇历史视图切换时保留独立归档但不写入另一报告视图，遇数据模式切换则丢弃。保存带AI报告只读取已有对应结果，不隐式再次调用付费AI。

`readiness(snapshot,internal)`只使用本地公开状态及prepared_date/calendar_loaded_date。时点由snapshot.now提供，日历必须今日核验；休市不要求本日终态。竞价活动时检查监测、关注池、接收年龄和覆盖，收尾后缺终态仅提示；自选模式不要求趋势池；未知mode明确错误。返回诊断是本机已知状态，不代表远程接口鉴权、上游时效或整段竞价已成功验证。

接入新数据源后至少用一个真实交易日验收：09:05前启动、09:10前完成准备；09:15检查首批；09:20检查阶段；09:25检查现有排名和终态覆盖；保存一轮耗时/每股覆盖；15:10检查日期与完整池。模拟回放只能证明程序顺序与算法边界，不能证明线上时延。

## 情绪结构、官方观察与请求可靠性（1.4）

`sentiment.py`使用报告的`raw.pools_by_date/raw.calendar`和当期涨停行，纯计算至多10日完整池梯队、封单留存分布、原因原文分组。`Service._review_sentiment()`按报告对象缓存；旧报告通过独立的`state.review_sentiment`展示，不为新增视图重写SQLite/JSON或改变AI证据身份。新`run_review`结果持久化`sentiment`。Markdown在旧报告缺字段时同样纯计算补视图，不发网络请求。

`Service.enrich_report(date,review_id)`是用户主动触发的`evidence`后台任务。提交时验证真实模式、已收盘、原报告日历及指纹；拒绝复盘正在生成和09:10–09:26。`official_context.py`只执行四个固定业务分项：竞价风向标，以及同日all/org/hot_money龙虎榜。每次provider.get之前检查取消；provider常规重试保留，因此HTTP尝试数可能超过四次。自动复盘、SSE、浏览历史、保存报告、AI分析均不会隐式启动这四项。

补充模块逐项保存原始data和受控错误，公开输出使用字段白名单；不读取成员表，不把龙虎榜归因为板块主力净流。风向标日期和date_ms必须完全吻合；龙虎榜trade_date核对且另检查零点timestamp，矛盾明确partial。失败分项为null，成功空榜为有效空数组；同股多区间和多记录保留，不猜测去重金额。各榜、各区间分别截取最多30行，raw保留完整结果。

任务完成后深拷贝原报告，添加`official_context/sentiment/enriched_at`，先预检Markdown渲染，再检查模式、保护时段及已存报告指纹；随后写JSON、SQLite，替换仍匹配的当前视图及latest_review、清除旧AI，最后写Markdown。Markdown文件写入失败时新证据和版本仍已同步，错误提示用户关闭占用文件后直接重试本机保存，不再次取数。各文件的原子替换不等于JSON、SQLite、Markdown整体原子事务。取消、全部不可用、并发报告更新或已有ready补充将被partial覆盖时，原报告保留。原有行情`report.status`与`official_context.status`独立，补充成功不升级旧盘面完整性。新证据改变`review_id`，原AI结果不沿用；历史视图与latest_review继续各司其职。

主程序和独立助手依然通过`llm.build_summary`输出同一递归白名单。新增`sentiment/official_observations`保留日期、样本、区间和缺失口径；龙虎榜每榜每区间进一步截取10条，不能称为完整名单。模型不接收raw、配置和未知上游字段。报告专属AI仍不混入今天竞价；DeepSeek、OpenAI和custom的配置、在途请求及结果隔离继续有效。

`provider.pool`验证分页回显page/size、total/pages算术一致性、页间总数稳定性、每页应有数量及代码唯一性；空池允许总页数0或1。全市场价格按目录总量翻页，不能把某页有效行情为空当作完成；证券目录也检查跨页重复。接口变动或部分缺页应失败，不能静默缩成完整样本。

限流采用同进程同Key共享冷却：HTTP429或业务4001记录下一可尝试时点，后续请求在冷却中快速返回受控错误，已有状态和SSE继续。Service另把已知冷却计入竞价调度，公开`api.rate_limited/cooldown_seconds`供页面显示。常规错误仍有有界重试；解析Retry-After是HTTP工程兼容，不是官网承诺。超过15秒的等待不会被截短成15秒再马上重试，而由调用方以后处理。竞价本身仍禁内部重试，冷却不改变逐批落盘、评分和发布顺序。

本轮复用入口是`sentiment.build_sentiment`与`official_context.build_official_context`，后者要求兼容`HiThinkProvider.get`且已校验HTTP200/code0的数据适配器。增改字段要同步CONTRACT、Markdown、页面、LLM白名单与临时夹具测试；官方参考资料位于ignored work目录，不能成为程序启动依赖。

## 历史研究与每日核验（1.5）

采集关键路径继续为逐批响应、原始保存、引擎更新和状态发布，模型与历史搜索不进入此路径。新批次保存 `_strategy_weights`；首次准备的日期、官方日历、完整前日池、采集范围与权重以 `INSERT OR IGNORE` 写入 `research_manifests`，之后重启或结果池不会覆盖当日盘前证据。两个固定截止时点保存引擎原排名到 `research_decisions`；遇下一批跨越截止时点，须先固定只含截止前数据的排名，再摄入该批。

`daily_validation.py` 在 09:27 后整理 09:24:50 与 09:26:00 档案。存在当时的时点排名时直接保留，标 `method=recorded_ranking`；没有时点排名但有原记录权重时，按所标识引擎回放，标 `method=replayed`。两时点可能不同来源；旧权重回退另有 `legacy_fallback`，不将重放值伪装为实时日志。`*-auction.json` 只写一次，收盘核验深复制后加标签并形成独立版本，不重算、覆盖冻结分数。

运行中的官方交易日自动调度 09:27 日档；开启 `auto_review` 后，在配置时间（默认 15:10）生成收盘复盘，再尝试匹配同日完整涨停池并启动本机研究。手动复盘亦会尝试核验；缺盘前清单、批次或原权重则 unavailable。保护时段和退出取消继续有效，自动任务不是硬性时刻保证，停机期间未采的数据不能恢复。

`research_data.validate_import` 对外部 JSON 作白名单、代码、时区、接收顺序、日期、单位与完整池声明验证；不请求网络、不补缺字段。HTTP 上限 5 MiB，每批文件最多 40 日，导入目录总计最多 120 日；导入与本机实盘同日冲突、导入版本冲突均拒绝覆盖。完整格式见 [BACKTEST.md](BACKTEST.md)。

`replay.replay_session` 是纯计算：原候选固定为昨日完整涨停池，过滤截止后批次，按留存顺序调用 `AuctionEngine`；结果池仅加入布尔标签。迟到准备或事后恢复上下文标 `reconstructed`，不能进入严格优化。`pool_transitions` 只在日历上相邻的完整池计算延续率与前五日背景，缺日期不跨越、不补零。

`optimization.optimize_sessions` 只接收合格的 09:24:50 会话。共同七因子完整样本至少 30 日、300 股日，开发集正负各至少 30；按日期三折验证，末尾 20% 至少 5 日只作一次检验。最多 64 组确定性权重实验，基线与候选的样本完全一致。此模块不取数、不写配置，候选建议不会自动应用。

`research.ResearchLibrary` 负责有界读取最近最多 120 日、每日本机最多 5,000 批，编排纯计算与归档。数据、源码与权重摘要组成 24 位十六进制实验 ID；相同输入复用实验。`holdouts.json` 保存已使用检验日期，复用时只标探索并拒绝接受建议。每个实验的 JSON/Markdown、开发/保留日期、固定种子、质量、指纹和缺失说明保留，修改当前参数不改过去证据。

`GET /api/research` 与独立下载返回完整结果，SSE 只增加 `research:{id,status,generated_at}` 与任务进度，避免每批推送大体积历史数据。`GET /api/research/daily` 提供目录或按日期读取；`GET /api/research/history?date=YYYY-MM-DD` 只读本机原始真实批次、清单和已知结果，按导入字段白名单导出。原始历史查询最多 5,000 批，保护时段拒绝这类重导出；缺结果池时 `outcome=null`，可供审计但不能直接作为完整导入文件。

`GET /api/research/ai-dataset?id=...` 只发开发日期，不给保留标签；开发不足可为空。`POST /api/research/proposals` 只归档七因子有限非负权重、模型来源、理由及实验/数据摘要，状态为 `awaiting_future_validation`。完整每日/实验/原始历史导出可能含保留期结果，不应用来继续自动调参。系统不隐式调用模型，也不自动将建议加入当前搜索或实盘配置；后续验证须使用新日期。

新增接口完整请求约束以 `CONTRACT.md` 为准。新增研究文件均在 `data/research/`，原始批次留在 SQLite，不因迭代实验清理；源码指纹不是源码备份，严格复现还应保存对应 Git 提交。

## 指定板块构建与证据档案（1.6）

`sector_research.py`只接收provider和捕获的完整基报告，不读取密钥、不落盘、不运行模型。行业与概念两份官方目录经代码/名称/类别校验后搜索；`Service._sector_catalog`用独立锁缓存15分钟。定向取数有单独非阻塞锁，避免同一时刻重复重型构建。请求前后通过`should_stop`检查模式、关闭、generation和09:10–09:26保护，不持有全局行情锁执行网络操作。

构建器每次核验官方日历，读取最多3板块的当前成员和60自然日指数日线。先使用基报告可核验日期的市场行情；最近收盘日不足样本补快照，旧日只补按代码顺序每板最多20个股票短日线。总最新快照代码最多300、历史短日线最多60；全部3板块构建上限为最新日10次或历史日67次业务调用，主服务目录未命中另2次，重试另计。成员归属和涨停交集以当前组成明确标记，无法还原历史指数成分。

`sector_library.py`深复制结果，绑定date/live/review_id后，以规范JSON SHA-256前24位保存独立证据；同id不重写，最新指针按报告日期与版本隔离。读取只接受固定id并验证内容摘要，文件上限8MiB。它不会修改原报告、原report_identity、竞价批次或股票池。SSE只附匹配当前报告版本的轻量证据摘要，详细数据经专用GET读取，避免每次竞价推送重复发送成员明细。

`run_llm(fetch_sectors=True)`固定所选模型配置和报告版本，先检查AI Key存在，再构建和保存证据；全部无可用证据、取消或模型调用前版本变化均阻止模型生成。部分可用证据连同覆盖及缺失进入`llm._targeted_sector_summary`独立白名单，最多3板块、各30同行和30涨停成员；未知raw/配置/敏感字段不进入提示词。JSON最多100/30行，UI最多20/20行，统计保留原样本口径，各层追加展示数量和截断提示。

AI结果记录本次问题及sector_codes/sector_evidence_id/sector_generated_at，仍按报告scope、日期、模式、provider和review_id绑定。Markdown只附已匹配回答及证据来源，不把补充反写进原基础报告；详细证据另存JSON。独立助手8766不新增隐式金融访问能力。完整接口及操作说明见 [SECTOR_RESEARCH.md](SECTOR_RESEARCH.md)。

## 独立 AI 应用（1.2）

`启动AI助手.cmd → launch_ai.py → run_ai.py → ai_studio.py` 单独监听127.0.0.1:8766，不创建行情Service或采集线程。静态界面为`ai.html/ai.css/ai.js`。主系统8765与助手8766共享用户级`ai-profiles.json`；网关使用跨进程文件锁与原子替换，避免两个界面保存时互相覆盖。

DeepSeek和OpenAI的地址固定；只有custom允许输入自定义地址。保存配置与选择服务商是两个操作，空Key保留同一家已保存值，不从其他服务继承。首次运行默认DeepSeek，旧版模型配置作为custom兼容读入。GET状态只返回has_key/configured等状态，不返回Key。主系统AI结果与独立助手历史都按服务商分隔。

发送聊天或市场分析时捕获服务商、模型和Key快照；执行途中切换不转移在途请求或回复。独立助手最多一个AI任务，按服务商持久化有限聊天历史。网络生成在后台线程执行，不持有竞价引擎锁；没有自动重试付费POST，没有失败后自动换服务商。

助手仅在用户加载摘要时访问固定本机8765状态，连接失败则检查已保存报告。递归白名单位于`llm.MARKET_FIELDS`，增加可发送字段必须人工核对；排除raw、config、Key、本机路径及全量历史。默认聊天不附摘要，勾选后只附用户已预览的那份快照。未知字段默认留在本机。报告缓存不能宣称实时竞价。

协议、运行步骤与官方依据见`docs/AI_ASSISTANT.md`；HTTP契约见`CONTRACT.md`。
