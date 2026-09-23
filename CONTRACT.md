# 模块与接口契约 · 1.7

Python 3.10+标准库，本机服务，无外部前端依赖。时间为上海UTC+08。共享可变状态由service锁保护；密钥不进入状态、文档或测试。字段缺失保留null，调用失败与成功空列表区分。

## Python模块

| 模块 | 接口与责任 |
| --- | --- |
| `provider.py` | `APIError`；`HiThinkProvider(api_key,min_interval=.5,timeout=6,max_retries=3)`；`get(path,params=None,max_retries=None)->dict` 返回校验过的data；认证只在请求头 |
| 基础数据 | `calendar()->list[str]` ISO日历；`tickers(asset_type='a-share')->list[dict]` 完整分页；`resolve_stock(code)->dict` 官方唯一精确A股匹配，含resolution来源；`stock_quote(codes)->dict` 单批显式代码快照 |
| 行情和复盘源 | `pool(kind,date)->list[dict]`，kind为limit-up/limit-down/limit-break且分页完整；`auction(codes,stage)->dict` 原始响应，最多100个原始代码token且内部不重试；`market()->dict` 所有页及page_timestamps；`catalog(tag)`、`indices(codes)`、`members(code)`、`ladder()` |
| 日线 | `historical(code,start,end,index=False,adjust='none')->dict`；股票趋势调用须显式adjust='forward'；`index_historical(code,start,end)`不带复权参数 |
| `engine.py` | `AuctionEngine(weights=None)`；`ingest(data,received_at,context=None)->list[dict]` 每一批后立即更新排名；`rankings(now=None)`、`summary()`、`reset()`；context按完整代码映射并带前一交易日日期。竞价量比缺失时按同会话≤`VOLUME_RATIO_CARRY_SECONDS`(600秒)有界携带，因子内带`value_source/value_age_seconds/carried_from`，`summary()`给`volume_ratio_carried_count` |
| `review.py` | `build_review(provider,date,previous_date,progress=None)->dict`；同步纯构建，service后台执行；保留raw来源，输出market/limit_up/ladder/sectors/trend/status/warnings |
| `stocks.py` | `validate_code(code)->str`仅格式规范化，不能代替官方核验；`analyze_stock(provider,metadata,date,calendar,context=None)->dict` 单股一次前复权日线、最多40交易日；不请求当前快照或竞价 |
| `selection.py` | `build_trend_pool(provider,date,calendar,report=None,progress=None,should_stop=None)->dict` 有限60候选/30入选；每次新取数前检查取消和09:10–09:26；只返回不写文件 |
| `service.py` | 后台任务、调度、同日恢复、按来源合并关注池、保护时段延后、JSON/SQLite持久化及SSE状态 |
| `reporting.py` | `report_identity(report)->str` 稳定公开证据 SHA-256；`render_markdown(report,ai=None,comparison=None)->str` 纯计算、安全转义的 Markdown |
| `report_library.py` | `ReportLibrary(store,data_dir)`；`list/get/markdown/save/read_markdown/save_ai/load_ai`，本机报告读取、原子保存和模式隔离，不调用行情接口 |
| `insights.py` | `compare_reports(current,previous)->dict` 两期证据比较；`readiness(snapshot,internal=None)->dict` 本机状态诊断，均无网络或磁盘 I/O、不读取系统时钟 |
| `sentiment.py` | `build_sentiment(report)->dict` 从留存完整池计算至多10日梯队矩阵、封单留存与原因原文分布；纯计算，无外部请求 |
| `official_context.py` | `build_official_context(provider,date,should_stop=None)->dict` 显式日期的风向标与三类龙虎榜，最多4个业务分项；收盘复盘自动调用，分项失败隔离、原始data保存在raw |
| `sector_research.py` | `load_catalog(provider,should_stop=None)`、`search_catalog(rows,query)`、`validate_sector_codes(values)`；`build_sector_research(provider,codes,date,report=...,catalog=None,now=None,should_stop=None,progress=None)` 有界指定板块取数，不落盘、不调用模型 |
| `sector_library.py` | `SectorLibrary(data_dir).save/get/latest`，按证据摘要保存和校验指定板块结果，独立绑定基报告版本 |

`review._price_trend`是stocks与selection共用的日线指标原语，但两模块的综合评分公式不同；不要因同名`_trend_score`而合并。竞价七因子又是独立公式，三种分数不可混排。

## 关注池与时点

默认focus = manual自选 + previous_limit_up昨日完整涨停 + strong_trend趋势强股，代码去重、来源可多选。watchlist模式仅自选；all模式另合入官方全市场目录。

增删自选不reset引擎，新加入者下一轮生效，之前未观察的数据不能补造。移除只取消manual来源；自动来源仍成立则保留跟踪。只使用截止日等于当前会话前一交易日的趋势池；收盘新池不反向替换当前会话的旧池。该会话副本同时写入`data/trend-pool-session.json`，因此同日重启（含收盘刷新之后）仍按原会话的昨日趋势来源重建关注池，不会退回只有昨日涨停的子集；快照日期与会话上一交易日不符时忽略，不猜测来源、不补造观察。

09:10–09:26（含整分钟）限制批量自选为每次1只，独立日线查询延后09:27，禁止开始手动趋势刷新和新复盘。趋势筛选遇保护/取消保留partial并稍后重试。已有本机观察及SSE继续；待处理个股查询只有一个槽位，新延后查询会替换旧查询。

竞价09:15–09:25逐批读取、落盘、计算、发布；09:25–09:26:50（`final_grace_seconds=110`）为终态复核窗口：即使全部代码已取得一次`matched/final`，仍按轮继续复核并在同一会话内采用最新值、逐批留证，因为官方首个终态响应可能仍是旧值。原始接口组装时间与本地接收时间区分，不能保证交易所源时间。日线截止日与本机竞价会话日分别展示。

## HTTP

主服务版本1.7.0。1.7新增同花顺接入，不改变既有金融数据口径。`GET /api/finance/status`与`state.finance`均返回以下无密钥对象；状态查询不联网：

```text
finance = {
  provider: 'hithink', label, base_url: 'https://fuyao.aicubes.cn', configured,
  credential_source: 'user_file'|'process'|'user_environment'|'missing', persisted,
  test: {status:'not_tested'|'running'|'success'|'error', ok:null|boolean,
         checked_at, message, latency_ms, calendar_count, latest_trade_date},
  can_save, save_block_reason, can_test, test_block_reason
}
```

`POST /api/finance/config {api_key}`只保存，返回`{ok,message,finance}`；非空ASCII字符串、最多512字符、禁止空白/控制字符。用户文件优先于进程与Windows用户环境，不改变全局环境。保存须停止监测、无活动任务、实盘模式且保护时段之外；清除旧验证和接入缓存，不采集。

`POST /api/finance/test {}`使用当前配置，返回`{ok,message,started,finance}`，结果在`finance.test`与`jobs.finance_test`推送。同名任务重复不重开，后台一次官方日历请求，max_retries=0；成功需HTTP200、code0与非空有效日期。测试不启动采集，不读取全市场，不调用模型；其他数据权限未由此证明。换Key或模式、保护时段变化使旧结果作废或取消，安全消息不含上游原始正文。验证状态仅在当前进程保留。

`POST /api/demo/exit {}`取消演示、清除合成内存并返回停止的live视图，只读已有本机实盘报告，不联网、不启动。已处于live时不改变运行状态。首次用户可先退出演示再配置Key。所有新POST沿用下述本机同源检查；接入操作详见`docs/FINANCE_CONNECTION.md`。

GET `/api/state`和SSE `/api/events`提供相同公开状态；SSE格式为`id: version`、`event: state`加JSON，重连建议为2000毫秒。状态版本变化立即推送完整状态，不等待固定刷新周期；闲时每次最长等待3秒后可发送注释keepalive，完整状态一般15秒刷新一次，09:10–09:26保护时段改为3秒，以更新时钟和陈旧提示。这是页面刷新策略，不改变竞价逐批计算或上游请求频率。GET `/api/history?symbol=完整代码`读取已有本机观察；GET `/api/health`主服务版本为1.7.0。

| POST路径 | JSON请求体及作用 |
| --- | --- |
| `/api/start`、`/api/stop`、`/api/prepare` | `{}`；开始/停止调度、后台准备 |
| `/api/service/restart` | `{}`；`Service.restart_prepare()` 先校验：09:10–09:26 拒绝、任一同名job为running拒绝；通过后`stop()`并返回`{ok,restarting,mode,message}`。处理器先回响应，再由**非daemon**后台线程关闭监听socket、关闭Store、派生新的`run.py`（日志追加`data/startup.log`、隐藏窗口、`[restart]`诊断行）并`os._exit(0)`；线程必须非daemon，否则`serve_forever`返回后主线程结束会把线程杀掉、子进程无法派生。`serve()`对绑定端口做最多8秒有界重试。`LocalServer(restart=None)`时端点返回400“未提供自动重启入口”，测试注入回调以验证触发但不真正退出进程；重启不迁移或改写任何证据，未采集数据不补造 |
| `/api/review` | `{date?:'YYYY-MM-DD'}`，只接受已收盘交易日 |
| `/api/reports/load` | `{date:'YYYY-MM-DD'}`；读取本机实盘历史报告，禁止演示中读取或复盘生成过程中切换，不请求行情 |
| `/api/reports/save` | `{date?,include_ai?:false,provider?,review_id?,baseline?}`；原子保存Markdown，include_ai必须为JSON布尔值；返回文件信息及校验下载地址 |
| `/api/reports/enrich` | `{date:'YYYY-MM-DD',review_id:'当前证据SHA-256'}`；异步补充指定已保存真实报告的官方观察；详见1.4契约 |
| `/api/demo` | `{}`，显式合成演示；与真实数据隔离 |
| `/api/config` | 非敏感配置变更；调整范围/权重须先停止。新增自选走核验端点 |
| `/api/watchlist/add` | `{codes:['000001','600519.SH']}`；也支持分隔字符串；1–50只，保护时段1只；异步核验并保存 |
| `/api/watchlist/remove` | `{code:'000001.SZ'}`；仅移除手动来源 |
| `/api/stocks/analyze` | `{code:'000001',date?:'YYYY-MM-DD'}`；独立研究，不自动加入自选 |
| `/api/trends/refresh` | `{}`；后台刷新最近已收盘日趋势池 |
| `/api/credentials` | `{api_key}`；保存用户级凭据并启动 |
| `/api/llm-config` | `{provider,model?,api_key?,base_url?}`；保存该家配置，不自动切换；缺provider兼容旧custom配置并选中 |
| `/api/llm-select` | `{provider}`；切换deepseek/openai/custom |
| `/api/llm-test` | `{provider?}`；后台GET模型列表检查，返回started |
| `/api/llm` | `{question?:'...',provider?,review_date?,review_id?,fetch_sectors?:false,sector_codes?}`；指定板块组合分析须fetch_sectors=true且有报告日期/版本，详见1.6契约；其余保持原摘要分析 |
| `/api/sectors/search` | `{query:'名称或完整代码'}`；1—80字，官方行业/概念完整目录匹配，返回ok/query/matches/exact/unmatched_note |
| `/api/sectors/research` | `{codes:[完整官方代码],date,review_id}`；1—3个唯一代码，异步取数，返回started；不调用模型 |

被拒绝的POST（Host/Origin/`X-Local-App`不合规或请求过大）先写出403/413响应，再做**有界且不阻塞**的请求体丢弃（最多1MiB、0.5秒socket超时）并带`Connection: close`关闭；Windows在未读请求体上关闭连接会产生TCP RST，客户端可能只看到连接中断而读不到拒绝原因，这条顺序用于避免该现象。响应为`{ok,message,...}`；review、reports/enrich、watchlist/add、stocks/analyze、trends/refresh、llm及llm-test另含started，false表示同名任务正在运行。仅允许本机Host、同源及`X-Local-App: auction-lab`。禁止GET暴露密钥、任意文件读取或向不同服务转发旧密钥。股票代码必须是字符串，保留前导零；裸代码不能仅凭已有完整代码缓存假定唯一。

| GET路径 | 查询参数与返回 |
| --- | --- |
| `/api/report` | `date?`、`format=json|markdown`（默认json）；date省略使用当前视图。JSON保留raw证据；Markdown另支持`include_ai=0|1,provider?,review_id?,baseline?`，直接下载不落盘 |
| `/api/reports` | 当前模式的本机目录`{mode,items:[{date,mode,generated_at}],limit:365}`，日期倒序；排除未来日期，不批量解码raw |
| `/api/reports/compare` | `date,baseline`，均为YYYY-MM-DD且baseline更早；仅比较当前模式本机保存报告，不补请求行情 |
| `/api/reports/download` | `filename,mode=live|demo,sha256?`；下载已保存的文件，sha256若提供必须匹配当前文件字节 |
| `/api/diagnostics` | `{status,summary,checked_at,checks:[{id,label,status,message}]}`，status及每项status为ok/warn/error/info；只检查本机已知状态 |
| `/api/sectors/research` | `date,review_id`；返回对应有效基报告版本最近一次板块证据或status=not_run，不联网 |
| `/api/sectors/export` | `evidence_id`；按24位小写十六进制编号校验并下载证据JSON，不接受路径 |

`/api/reports/save`成功返回`{ok,message,path,filename,date,mode,review_id,sha256,download_url}`。前端使用服务返回的download_url取得本次保存的同一内容，不自行拼路径。文件在点击后又被重新生成或保存覆盖时，带旧sha256的下载必须失败并要求重新保存；校验值不是访问凭据。API日期须严格匹配有效YYYY-MM-DD，不接收任意路径。文件名只允许`YYYY-MM-DD.md`或`YYYY-MM-DD-with-ai-{deepseek|openai|custom}.md`；实盘目录`data/reports/`，演示目录`data/reports/demo/`。Markdown读取上限4,000,000字节，JSON文件回退读取上限64,000,000字节。

## 状态与结果

```text
state = {
 mode:'live'|'demo', status,message,now,configured,running,
 calendar:{dates,today_is_trading,checked_on},
 auction:{date,phase,rows,summary,universe_count,processed_count,
          cycle_seconds,last_batch_ms,finalized,final_count,coverage,source_counts},
 stocks:{query_code,analysis,watchlist,trend_pool},
 review:null|report, review_id:null|sha256, review_sentiment:null|sentiment,
 sector_research:{evidence_id,date,review_id,status,generated_at}|{},
 api:{rate_limited:boolean,cooldown_seconds:number}, jobs:{}, config:publicConfig,
 finance:{provider,base_url,configured,credential_source,persisted,test,can_save,save_block_reason,can_test,test_block_reason},
 research:{id?,status,generated_at?},
 llm:{active_provider,profiles,configured,base_url,model,label,result,connection_test},errors:[]
}
```

公开calendar.dates只显示最近15个交易日，不能当作筛选/均线所需完整日历。jobs以prepare/review/evidence/sectors/llm/llm_test/stock/watchlist/trends/research/daily_validation/finance_test等为键，值为`{status:'running'|'done'|'error',message}`。POST返回`started=true`仅表示后台任务已创建；调用方须等对应job为done后读取结果。任务error时旧成功档案仍可能存在，不能冒充本轮结果。job完成也不代表数据完整：板块证据可为partial/unavailable，保护时段个股analysis.status可以为deferred。api冷却为本机已知的剩余等待，不代表外部服务保证恢复时间。

竞价rows含`rank,thscode,name,score,factors,quality,auction_pct,auction_amount,updated_at,phase,sources`；公开状态去掉raw和大体积历史，详细本机轨迹另取。

个股analysis含`thscode,ticker,name,metadata,date,status,trend,trend_score,trend_factors,trend_coverage,history,warnings,context,score_method,source`。service附加`auction:{row,history,date,mode,tracking,scope_count}`。analysis.date与auction.date分别是日线截止日/竞价会话日。watchlist行含`thscode,name,sources,tracking`。

趋势池含`date,status,rows,candidate_count,candidate_available_count,evaluated_count,valid_history_count,selected_count,matched_count,coverage,warnings,generated_at,completed_at,method,thresholds`。rows含`rank,thscode,name,date,score,trend_score,trend,reason,source`；score与trend_score同值。trend附`score_factors,score_weights,score_factor_coverage_pct`，覆盖按权重计算。coverage保留池日数、历史失败、短历史、未评估、有限样本和取消保护标记。

默认publicConfig见config.py：poll_seconds=3、batch_size=100、universe='focus'、watchlist=[]；七因子权重gap=.15、amount=.15、turnover=.10、volume_ratio=.10、late_momentum=.20、retention=.15、continuity=.15。缺因子不补零。

## 报告版本、AI附录与两期比较（1.3）

`review_id`是报告公开证据的规范JSON SHA-256，递归排除raw、敏感键及导出/AI/对比元数据；包含generated_at/completed_at等来源时间。相同日期重新生成不是同一版本。Service按报告对象身份缓存此值：**报告对象发布后不可原地修改**，只能构建新对象再替换；否则缓存会失效。保存与专属AI请求应同时提交当前日期、provider和review_id，版本不匹配立即拒绝。

`review`是用户当前查看的报告；内部`latest_review`是自动复盘与趋势刷新所依据的最新实盘报告。读取历史只切换review并设置viewing_archive，不能改变latest_review。自动复盘更新latest_review和存储，但用户查看历史时不强行跳转；手动生成完成会切换至新报告。自动任务不能因历史视图日期较旧就重复生成当日报告。

AI结果为`{text,generated_at,mode,provider,label,model,scope,review_date,review_id,question}`，1.6指定板块另含`sector_codes/sector_evidence_id/sector_generated_at`。question最多2000字符。`scope='market'`可含当前竞价和个股摘要，不可作为某天报告附录；`scope='review'`发送指定报告白名单摘要，可加显式请求的同日板块证据，竞价为空且phase=not_included，个股分析为空。提交时固定服务商/配置/报告版本。按`data/ai-reviews/{live|demo}/YYYY-MM-DD-{provider}.json`原子归档，只写结果白名单，不包含配置或凭据；读取上限1,000,000字节。恢复或附录必须同时匹配scope、mode、review_date、review_id、provider。用户切换历史视图后，旧在途报告AI可保留其独立归档，但不得显示到另一报告下；模式改变则丢弃结果。同日同服务商归档保存最近一次结果，版本不匹配不得沿用。

两期比较主要结构：

```text
comparison = {
 status:'ready'|'partial'|'unavailable', current_date,previous_date,mode,
 adjacent_sessions:true|false|null,warnings,
 market:{status,rows:[{id,label,unit,current,previous,delta,status}]},
 limit_up:{status,current_count,previous_count,retained_count,new_count,exited_count,
   retained:[{thscode,name,current:{score,consecutive_days,consecutive_lower_bound}|null,previous:{...}|null}],
   new:[...],exited:[...],coverage:{current_complete,previous_complete,current_valid_rows,previous_valid_rows},definition},
 sectors:{status,rows:[{thscode,name,current_rank,previous_rank,rank_change,
   current_change_pct,previous_change_pct,change_delta_pp,
   current_turnover,previous_turnover,turnover_change_pct,status}],
   coverage:{current_count,previous_count,common_count,comparable_count,returned_count},warnings}
}
```

指标行status为ready/provisional/unavailable；数量为家或板、成交额为元、比例差为百分点。板块rank_change=基准名次−当期名次，正值表示样本内名次前进，不能解释为净资金。先按完整代码匹配全部共同板块，再展示涨幅变化绝对值前30项。涨停池必须ready、数量相符、无重复且完整代码有效，才能判共同两期涨停/当期新出现/从对比池退出；空完整池为0，失败池为null。相邻性只从当期raw.calendar核验，不凭自然日差推断；即使共同两期出现也不自行推定中间连续涨停。严格连板只用consecutive_days并保留下限。日期不对应不计算变化，推断日期或不完整盘面标provisional。成交额基准为0时相对变化为空。

诊断时点来自snapshot.now，内部只补prepared_date/calendar_loaded_date。交易日历必须今日核验且成员与today_is_trading一致；不以周末或工作日替代官方日历。竞价时段检查关注池、监测开关、本地接收年龄和覆盖；收尾后缺终态为warn，休市日不要求终态。仅自选模式趋势池为info；未知mode直接error。结果是已知状态检查，不证明远程鉴权成功、未来可用性或十分钟完整覆盖。

## 情绪结构与官方补充观察（1.4）

`review_sentiment`为当前报告的本机纯统计视图，优先复用报告已有`sentiment`，旧报告则按不可变报告对象缓存计算。浏览旧报告不写库、不请求金融接口、不改变`review_id`；新生成报告会持久化`sentiment`。统计结果结构：

```text
sentiment = {
 version:'1.0',date,mode,status,definition,warnings,
 matrix:{status,rows:[{date,status,limit_up_count,buckets:{1,2,3,4,5+,unknown},
   lower_bound_count,lower_bound_by_bucket,max_consecutive,coverage,warnings}],
   coverage:{expected_days,complete_days,coverage_pct,calendar_verified,requested_window_days:10},
   definition,warnings},
 retention:{status,total_count,observed_count,valid_count,missing_count,invalid_count,
   above_100_count,excluded_conflict_count,median_pct,below_50_count,below_50_pct,
   coverage_pct,definition,warnings},
 reasons:{status,source_field:'limit_up_reason',total_count,observed_count,known_count,
   missing_count,invalid_count,excluded_conflict_count,group_count,displayed_count,coverage_pct,
   rows:[{reason,count,share_pct,codes,displayed_code_count,other_code_count}],definition,warnings}
}
```

矩阵至多10个目标日前交易日，完整池缺失或代码重复/无效时计数为null。`buckets`是字符串键且互斥；无法确认的高度单列unknown，窗口下限另计，1板下限不等于首板。封单分布剔除当前封单额大于峰值的异常；`above_100_count`独立于其他数值异常计数。原因保留完整原文，最多30组、每组最多10个代码，`group_count`保留总组数并对未展开部分给出提示，不解释为行业。

`POST /api/reports/enrich`只允许live、非未来且已收盘、具备`raw.calendar`中目标日证据的已保存报告；报告版本必须匹配，09:10–09:26与复盘生成期间拒绝开始。返回`{ok,message,started}`，任务为`jobs.evidence`。按顺序读取显式`date`的风向标和`board_type=all/org/hot_money`三榜，每次新分项请求前检查模式、关闭和保护时段。`requests.attempted`统计业务分项调用，**不是HTTP尝试数**；provider可以按现有策略重试，最多4个分项不等于总共4次HTTP请求。

```text
official_context = {
 date,mode:'live',generated_at,status:'ready'|'partial'|'unavailable'|'cancelled',
 cancelled,requests:{attempted,max:4},definition,warnings,
 errors:[{section,kind,code,message}],raw:{benchmark?,all?,org?,hot_money?},
 benchmark:null|{status,date,date_ms,response_timestamp,endpoint,sample_count,
   valid_auction_count,mean_auction_pct,median_auction_pct,positive_ratio_pct,
   rows:[{thscode,ticker,name,auction_pct,tags,record_index}],shown_count,truncated,sort,definition,warnings},
 dragon_tiger:{all:null|board,org:null|board,hot_money:null|board}
}
board = {
 status,board_type,trade_date,timestamp,timestamp_matches_date:true|false|null,
 date_basis:'explicit_trade_date',endpoint,reported_count,reported_stock_count,
 received_rows,observed_stock_count,groups:{one_day:[row],three_day:[row],other:[row]},
 group_counts:{one_day,three_day,other},shown_count,truncated,duplicate_record_count,
 actors:[{name,reported_net_value,row_count}],actor_count,actors_truncated,sort,definition,warnings,
 coverage:{status:'verified'|'inconsistent'|'unknown',scope,
   record_count_matches:true|false|null,stock_count_matches:true|false|null,definition}
}
```

风向标验证`date`与上海零点`date_ms`完全一致；timestamp只表示组装时间。均值、中位数、上涨占比仅统计有效竞价涨幅，真实0保留，`auction_pct`本身已经是百分数。样本缺失则统计null；重复代码使该分项不可用。公共行按涨幅降序、代码升序最多30只，完整数据留raw。

龙虎榜核对`trade_date`和`board_type`，timestamp与该日零点交叉检查；不一致或缺失使分项partial，但保留明确trade_date来源，原始时间不改写。`count`是上游记录数，`stock_count`是去重股票数，不能互相替代。每区间最多30行，截取前数量在`group_counts`；全部榜按`net_value`、机构榜按`org_net_value`、游资榜按`hot_money_item_net_value`降序，缺值末尾，同值按代码/游资名称/原序号稳定排序。

all/org的coverage按`scope='stock_items'`核对声明总条数与实际行数、声明去重股票数与有效代码去重数；不符则inconsistent并使分项partial，不能声称完整空榜。hot_money的coverage为`scope='hot_money_items.rows',status='unknown'`，两个matches均null：上游计数与游资展开样本范围不同，两者差异本身不作为丢数据证据；游资分项ready也不表示已证明全量覆盖。

`row`采用白名单：`thscode,ticker,name,range_days,limit_reason,concept_list`，其中limit_reason是涨跌停原因；金额元字段`net_value,org_net_value,hot_money_net_value,hot_money_item_net_value,buy_value,sell_value,amount`；原小数字段`change,net_rate,org_net_rate,hot_money_net_rate,hot_money_item_net_rate`及相应`change_pct,net_rate_pct,org_net_rate_pct,hot_money_net_rate_pct,hot_money_item_net_rate_pct`百分数字段；`hot_rank,org_buy_num,org_sell_num,hot_money_name,reported_hot_money_net_value,record_index,duplicate_record,quality`。游资从`hot_money_items[].rows`展开，上层`buying`保存为聚合净额，不能按子行再次累加。同股票同区间多记录保留并标注；不计算三榜、不同区间或概念金额合计。

取消、全部不可用或持久化前版本冲突时原报告保留；原有ready补充不被新partial替换。可用补充通过完整报告深拷贝写入`official_context`、`sentiment`、`enriched_at`，不改变原复盘`status`。更新的是报告版本，不是盘前选股或竞价排名；旧AI附录随review_id变化失效。

生成复盘时（自动15:10与手动相同）`Service.run_review`也按这套显式日期、逐分项可取消的规则读取最多4个业务分项：可用则直接写入新报告的`official_context`，`partial`另加“部分缺失”警告，`unavailable`/`cancelled`/异常只加警告并保持原盘面状态；报告照常保存，`label()`与本地研究继续执行。龙虎榜通常在收盘当晚才发布，15:10 自动取得的分项可能暂为未就绪，可稍后点「联网补充官方观察」改为ready版本（旧ready不会被新partial覆盖）。

提交前预检Markdown渲染；保存证据并同步内存版本、清除旧AI后才写Markdown文件。文件被占用等写入失败不回滚已保存证据，会提示可直接重试本机导出；不能声称JSON、SQLite、Markdown整体构成原子事务。

Markdown包含情绪结构、已补充观察及缺失口径。LLM经`build_summary`导出`sentiment`和`official_observations`显式白名单，不发送raw；列表最多60项（超过15万字节时依次降到30、10，并在`scope_note`声明），榜单另附`limit_up_scope`/`sectors_scope`的shown/available/total/truncated，龙虎榜每榜每区间进一步限10条，保留真实总数与截取提示。原有DeepSeek/OpenAI/custom服务商隔离、版本绑定和用户主动发送规则继续适用。

`HiThinkProvider.rate_limit_status()->{rate_limited,cooldown_seconds}`只读本机状态；`APIError.retry_after_seconds`是安全数值元信息。同进程同Key的实例共享429/4001冷却，冷却中请求快速返回受控错误。常规重试尊重可解析的Retry-After；要求等待超过15秒时不截短后立即重试，而交给调用方稍后处理。竞价仍每批单次HTTP尝试。该兼容机制不表示官网保证提供Retry-After，也不保证特定吞吐或恢复时效。

## 指定板块证据与模型组合任务（1.6）

完整字段、示例和业务上限见 [docs/SECTOR_RESEARCH.md](docs/SECTOR_RESEARCH.md)。取数须live、官方日历确认已收盘、当日达到15:10，并绑定所选报告的有效`review_id`。`fetch_sectors`须JSON布尔值；携带`sector_codes`但未开启取数时拒绝。组合任务先检查模型Key存在，再读取官方数据；全无有效证据时归档unavailable但不调用模型，部分有效则携带缺失继续。

行业和概念目录各全量读取，主进程缓存15分钟；代码须匹配该官方目录，纯代码、未知代码及重复选择拒绝，不根据别名猜映射。每次最多3板块，最新报价合并最多300代码、每批100；历史行情每板按代码顺序最多20，合并最多60个短日线请求。构建最多10次最近日或67次历史日业务调用，目录未命中另2次，provider重试另计。每次请求前后检查保护/取消，09:10–09:26不开始新的板块联网任务，竞价每批流程不调用该模块。

结果含`date/mode/review_id/evidence_id/generated_at/status/boards/coverage/warnings/definition`；board保留当前成员依据、取数时间、日期及行情覆盖、指数趋势、样本统计、同行明细和涨停交集。当前成员不是历史组成；快照不能替代旧日行情；完整收盘池缺失时涨停未知；严格连板另有consecutive_known_count；net_flow保持null。JSON每板100同行/30涨停，页面20/20，LLM60/60；各层明示截断并保留统计原分母。模型经独立分层白名单`targeted_sectors`接收所选板块，不受`sectors_top30`排除。

`SectorLibrary`保存`data/sector-research/evidence/<id>.json`及`latest/<date>-<review_id>.json`指针。id为证据（排除自身id）的规范JSON SHA-256前24个十六进制字符，包含取数时间；校验读取上限8MiB。基报告对象及其review_id不变，旧证据可按id审计；不将新数据原地附加进基报告。报告重新生成后，原证据不自动成为新版本证据。模型发起前再次确认版本，AI附录保留问题、代码、证据编号与时间且全部转义。独立助手不因聊天或加载摘要隐式访问金融API。

## 持久化与维护

- SQLite按live/demo隔离批次及报告；JSON复盘保留raw。公开状态递归去掉raw，不代表原始证据丢弃。
- `data/stock-metadata.json`缓存官方解析；`data/trend-pool.json`缓存筛选；`data/trend-pool-session.json`保存本次会话所用昨日趋势池副本（仅供同日重启保留原会话来源，日期不符即忽略）；`data/stocks/完整代码-日期.json`保存个股日线分析。
- `_complete_trend_cache`允许复用因日期推断或短历史而partial、但全部候选处理完且池齐全/无请求失败/无保护中断的同日缓存。中断或失败不能冒充完成；最新复盘完成后仍主动重筛。
- 演示拒绝真实查询/自选变更/趋势刷新，不能将真实个股或趋势池显示为演示信号。
- 原生中文前端无CDN；保持查询/加入关注分开，显示日线日期、竞价日期、模式、质量及评分口径。
- unittest使用自包含夹具、临时目录；真实认证、真实交易时段和离线边界测试的验收分别记录。修改同步README、策略、数据源、架构、AGENTS及本契约。
- 面向其他AI的执行入口为`docs/AI_MAINTENANCE.md`，全部本机端点和内部扩展接口见`docs/API_REFERENCE.md`，每日数据到外部参数建议的治理流程见`docs/CONTINUOUS_OPTIMIZATION.md`。接口简表与详细文档必须同次更新。

## 独立助手 HTTP（1.2，端口8766）

健康标识为`auction-ai-assistant`，主系统标识仍是`auction-lab`。助手仅接受本机Host、同源Origin、POST请求头`X-Local-App: auction-ai`；JSON体上限65536字节。助手不启动Service或金融数据请求。

| 方法与路径 | 请求/作用 |
|---|---|
| GET `/api/health` | `{ok,application,version}` |
| GET `/api/state` | `{ai,messages,job,connection_test,context}` |
| POST `/api/profiles/save` | `{provider,api_key?,model?,base_url?}`；保存不切换，空Key保留 |
| POST `/api/profiles/select` | `{provider}`；切换当前服务商 |
| POST `/api/profiles/test` | `{provider?}`；后台仅GET/models，不生成回答 |
| POST `/api/context/load` | `{}`；本机加载并保留预览快照 |
| POST `/api/chat` | `{message,include_market:false,provider?}`；默认不附盘面，最多8000字 |
| POST `/api/chat/clear` | `{provider?}`；清空当前服务商记录，忙时拒绝 |

`ai={active_provider,profiles:[{id,label,base_url,model,protocol,configured,has_key,models,key_source}]}`。无API Key字段。`messages`只展示当前provider，消息含role/content/provider/model/created_at/warnings。后台job含status/id/kind/provider/message；connection_test含ok/provider/model/model_available/message。列表可读不等于生成权限或余额已验证。

`context={summary,loaded_at,source,mode,date,cached,message}`。source为local_service或local_report；summary保留auction_date和review.date等具体日期。读取主系统使用固定本机地址，不接受用户传入URL。缓存报告不是实时竞价；没有可用数据时仍可普通聊天。

聊天提交时复制配置、当前provider历史及可选摘要。结果回写提交时provider，不受之后切换影响。HTTP已有任务运行时返回started=false；任务中的接口错误只返回安全摘要。主系统AI结果也按provider保存，切换不会把另一家的结果显示成当前模型生成。

生成和清空请求显式传页面当前provider，避免另一个窗口改变全局选择后、轮询尚未刷新的短窗口错误路由。缺provider的旧客户端才采用当前全局选择。
# 1.5 历史研究与每日核验接口补充

以下接口仅本机使用；POST继承`Host/Origin/X-Local-App: auction-lab`检查。GET不调用金融API或LLM。原有契约继续有效。

| 接口 | 请求 / 响应 |
|---|---|
| GET `/api/research` | 最近实验对象，未运行为`{status:"not_run"}`。含availability、transitions、sessions、optimization、warnings、reproducibility。 |
| POST `/api/research/run` | `{}`；后台research任务，返回ok/message/started。保护时段或demo拒绝。 |
| GET `/api/research/export?id=<24hex>&format=markdown\|json` | 不可变实验下载，id可省略取最近，禁止路径输入。实验本身已经自动保存。 |
| GET `/api/research/template` | schema_version=1空模板及字段说明，不含行情。 |
| POST `/api/research/import` | `{dataset:{schema_version,provenance,sessions}}`；最多5MiB/40日，每日5000批/每批100股，累计120日。日期不可覆盖或与本机live混用；返回import_id/imported_days。 |
| GET `/api/research/daily` | `{items:[...]}`每日档案目录。 |
| GET `/api/research/daily?date=YYYY-MM-DD` | 最近核验版本，否则校正版本，再否则冻结竞价档；无档为unavailable。sessions区分method=recorded_ranking/replayed，校正版本另带corrects_frozen_id/reason/supersedes。 |
| GET `/api/research/daily/export?date=...&format=markdown\|json` | 下载所选版本日档，无档不造报告；markdown按同一对象渲染排名汇总。 |
| GET `/api/research/history?date=...` | 真实本机清单＋原始竞价序列白名单＋结果池；无清单/批次拒绝；结果池缺失为null。保护时段拒绝。 |
| GET `/api/research/ai-dataset?id=<24hex>` | schema_version=1，scope=development_only，仅开发日期09:24:50、合格来源的rows/factors/label；不含保留日期明细或09:26终态。数据不足可能sessions为空。 |
| POST `/api/research/proposals` | `{experiment_id,weights:{全部七键},source_model?,rationale?}`；仅归档，返回proposal_id/status=awaiting_future_validation/automatically_applied=false。不改配置、不调用付费模型。 |
| GET `/api/research/proposals` | 最近最多100份待验证参数建议，供继续研究追溯；不代表已通过验证。 |

其余POST仍为64KiB上限。所有证券代码均为六位字符串加.SH/.SZ/.BJ；不从裸代码猜市场。导入provenance须明确观察时间、元金额、百分数单位和`point_in_time_attested=true`，不等于程序认证外部历史真实性。完整导入细节见`docs/BACKTEST.md`。

`/api/state.research`仅含id/status/generated_at，不把全实验随SSE广播。每日自动核验在15:10后的收盘复盘完成后触发；核验失败单独报告，不抹去已完成的复盘。原始SQLite、竞价日档、实验JSON、Markdown及建议档案在本机data目录隔离保存。优化结果最多说明此数据和预设规则下的比较，不输出交易收益承诺。

每日档案的 Markdown 汇总随冻结自动生成：09:27 写出 `data/research/daily/YYYY-MM-DD-auction.md`，15:10 后另写核验版本的 `YYYY-MM-DD-<24位ID>.md`；两者都由 `render_daily(record)` 纯计算渲染同一对象，不新增行情请求，也不再需要用户先点「保存 Markdown」。冻结与同名 Markdown 都只写一次，后续算法变化不会覆盖既有日档。

09:25 后约一分钟先写实时视图：`Service._publish_morning_ranking(current)` 只在 09:26 分钟调用（同一分钟内最多每 20 秒一次），先由既有`_capture_due_decisions`补记到期时点快照（`research_decisions`的`INSERT OR IGNORE`语义不变），再以引擎当前逐批排名调用 `DailyValidation.publish_morning_ranking(day,now,session)`。该方法用 `render_morning_ranking(record)` 渲染并**原子替换** `data/research/daily/YYYY-MM-DD-morning-ranking.md`，记录 `kind=live_ranking_view`、`generated_at`、`checkpoint=09:26:00`、`method=live_ranking`，每行保留分数、`provisional_score`、因子覆盖、观察批次、数据阶段与是否为昨日涨停候选。它明确声明自己是可刷新视图：不新增`batches`记录、不创建或改写任何`*-auction.json`、不参与优化样本，也不能被当作原始评分证据；`/api/research/daily`与日档导出只读取冻结、校正与核验版本。

冻结后才发现当日整日不可评分（例如上游阶段命名不在显式白名单内）时，`DailyValidation.correct(date,now,reason)` 按当日原始批次、当时权重与当前引擎源码重放，并写独立校正版本 `YYYY-MM-DD-<24位ID>.json/.md`：保留`corrects_frozen_id`、`reason`、`supersedes`、`correction_source_sha256`与`batch_count`，不修改原冻结文件；重放后仍无可评分记录则拒绝写入。随后的15:10标签只在同一冻结档案的校正版本确有可评分行时使用它，并记录`scores_basis=correction`、`scores_source_id`及原冻结`auction_frozen_id`。维护入口是 `python tools/rebuild_daily_ranking.py --date YYYY-MM-DD --reason "..."`：只读本机SQLite与批次，不联网、不调用模型、不写`batches`/`research_manifests`/`research_decisions`，因而不产生可冒充原始证据的新记录。

冻结档案与校正版本都带 `field_coverage`：按当日原始批次统计`FIELD_AUDIT_FIELDS`（`auction_volume_ratio`、`auction_turnover_pct`、`auction_yesterday_ratio_pct`、`auction_unmatched`、`open_price`）在09:15–09:26窗口内“有值批次/首次/最后有值时间”，并按引擎“最新一次观察”语义给出两个时点各有多少候选真正取值。键缺失与null都不补造；该字段用于区分上游停发与本机解析问题（2026-09-21/22实测量比只在开盘首条快照、个别换手缺失个股和完整终态整批响应中出现），也是判断某日能否进入七因子共同样本的审计依据。

当前没有提案验证、批准、拒绝、应用或回退端点；`POST /api/research/proposals`固定只归档`awaiting_future_validation`，`POST /api/config`也不会关联或更新提案。每日滚动研究在留出日期与`holdouts.json`既有日期重叠时降为`exploratory_holdout_reuse`，不能声称新的独立验证。持续优化的人工治理和未来闭环约束见`docs/CONTINUOUS_OPTIMIZATION.md`。
