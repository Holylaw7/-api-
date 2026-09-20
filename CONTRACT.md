# 模块与接口契约 · 1.4

Python 3.10+标准库，本机服务，无外部前端依赖。时间为上海UTC+08。共享可变状态由service锁保护；密钥不进入状态、文档或测试。字段缺失保留null，调用失败与成功空列表区分。

## Python模块

| 模块 | 接口与责任 |
| --- | --- |
| `provider.py` | `APIError`；`HiThinkProvider(api_key,min_interval=.5,timeout=6,max_retries=3)`；`get(path,params=None,max_retries=None)->dict` 返回校验过的data；认证只在请求头 |
| 基础数据 | `calendar()->list[str]` ISO日历；`tickers(asset_type='a-share')->list[dict]` 完整分页；`resolve_stock(code)->dict` 官方唯一精确A股匹配，含resolution来源；`stock_quote(codes)->dict` 单批显式代码快照 |
| 行情和复盘源 | `pool(kind,date)->list[dict]`，kind为limit-up/limit-down/limit-break且分页完整；`auction(codes,stage)->dict` 原始响应，最多100个原始代码token且内部不重试；`market()->dict` 所有页及page_timestamps；`catalog(tag)`、`indices(codes)`、`members(code)`、`ladder()` |
| 日线 | `historical(code,start,end,index=False,adjust='none')->dict`；股票趋势调用须显式adjust='forward'；`index_historical(code,start,end)`不带复权参数 |
| `engine.py` | `AuctionEngine(weights=None)`；`ingest(data,received_at,context=None)->list[dict]` 每一批后立即更新排名；`rankings(now=None)`、`summary()`、`reset()`；context按完整代码映射并带前一交易日日期 |
| `review.py` | `build_review(provider,date,previous_date,progress=None)->dict`；同步纯构建，service后台执行；保留raw来源，输出market/limit_up/ladder/sectors/trend/status/warnings |
| `stocks.py` | `validate_code(code)->str`仅格式规范化，不能代替官方核验；`analyze_stock(provider,metadata,date,calendar,context=None)->dict` 单股一次前复权日线、最多40交易日；不请求当前快照或竞价 |
| `selection.py` | `build_trend_pool(provider,date,calendar,report=None,progress=None,should_stop=None)->dict` 有限60候选/30入选；每次新取数前检查取消和09:10–09:26；只返回不写文件 |
| `service.py` | 后台任务、调度、同日恢复、按来源合并关注池、保护时段延后、JSON/SQLite持久化及SSE状态 |
| `reporting.py` | `report_identity(report)->str` 稳定公开证据 SHA-256；`render_markdown(report,ai=None,comparison=None)->str` 纯计算、安全转义的 Markdown |
| `report_library.py` | `ReportLibrary(store,data_dir)`；`list/get/markdown/save/read_markdown/save_ai/load_ai`，本机报告读取、原子保存和模式隔离，不调用行情接口 |
| `insights.py` | `compare_reports(current,previous)->dict` 两期证据比较；`readiness(snapshot,internal=None)->dict` 本机状态诊断，均无网络或磁盘 I/O、不读取系统时钟 |
| `sentiment.py` | `build_sentiment(report)->dict` 从留存完整池计算至多10日梯队矩阵、封单留存与原因原文分布；纯计算，无外部请求 |
| `official_context.py` | `build_official_context(provider,date,should_stop=None)->dict` 显式日期的风向标与三类龙虎榜，最多4个业务分项；分项失败隔离、原始data保存在raw |

`review._price_trend`是stocks与selection共用的日线指标原语，但两模块的综合评分公式不同；不要因同名`_trend_score`而合并。竞价七因子又是独立公式，三种分数不可混排。

## 关注池与时点

默认focus = manual自选 + previous_limit_up昨日完整涨停 + strong_trend趋势强股，代码去重、来源可多选。watchlist模式仅自选；all模式另合入官方全市场目录。

增删自选不reset引擎，新加入者下一轮生效，之前未观察的数据不能补造。移除只取消manual来源；自动来源仍成立则保留跟踪。只使用截止日等于当前会话前一交易日的趋势池；收盘新池不反向替换当前会话的旧池。

09:10–09:26（含整分钟）限制批量自选为每次1只，独立日线查询延后09:27，禁止开始手动趋势刷新和新复盘。趋势筛选遇保护/取消保留partial并稍后重试。已有本机观察及SSE继续；待处理个股查询只有一个槽位，新延后查询会替换旧查询。

竞价09:15–09:25逐批读取、落盘、计算、发布；默认终态补采至09:26。原始接口组装时间与本地接收时间区分，不能保证交易所源时间。日线截止日与本机竞价会话日分别展示。

## HTTP

GET `/api/state`和SSE `/api/events`提供相同公开状态；SSE格式为`id: version`、`event: state`加JSON，重连建议为2000毫秒。状态版本变化立即推送完整状态，不等待固定刷新周期；闲时每次最长等待3秒后可发送注释keepalive，完整状态一般15秒刷新一次，09:10–09:26保护时段改为3秒，以更新时钟和陈旧提示。这是页面刷新策略，不改变竞价逐批计算或上游请求频率。GET `/api/history?symbol=完整代码`读取已有本机观察；GET `/api/health`主服务版本为1.4.0。

| POST路径 | JSON请求体及作用 |
| --- | --- |
| `/api/start`、`/api/stop`、`/api/prepare` | `{}`；开始/停止调度、后台准备 |
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
| `/api/llm` | `{question?:'...',provider?,review_date?,review_id?}`；无review_date为盘面分析，有review_date为该版本收盘报告专属分析，详见下文 |

响应为`{ok,message,...}`；review、reports/enrich、watchlist/add、stocks/analyze、trends/refresh、llm及llm-test另含started，false表示同名任务正在运行。仅允许本机Host、同源及`X-Local-App: auction-lab`。禁止GET暴露密钥、任意文件读取或向不同服务转发旧密钥。股票代码必须是字符串，保留前导零；裸代码不能仅凭已有完整代码缓存假定唯一。

| GET路径 | 查询参数与返回 |
| --- | --- |
| `/api/report` | `date?`、`format=json|markdown`（默认json）；date省略使用当前视图。JSON保留raw证据；Markdown另支持`include_ai=0|1,provider?,review_id?,baseline?`，直接下载不落盘 |
| `/api/reports` | 当前模式的本机目录`{mode,items:[{date,mode,generated_at}],limit:365}`，日期倒序；排除未来日期，不批量解码raw |
| `/api/reports/compare` | `date,baseline`，均为YYYY-MM-DD且baseline更早；仅比较当前模式本机保存报告，不补请求行情 |
| `/api/reports/download` | `filename,mode=live|demo,sha256?`；下载已保存的文件，sha256若提供必须匹配当前文件字节 |
| `/api/diagnostics` | `{status,summary,checked_at,checks:[{id,label,status,message}]}`，status及每项status为ok/warn/error/info；只检查本机已知状态 |

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
 api:{rate_limited:boolean,cooldown_seconds:number}, jobs:{}, config:publicConfig,
 llm:{active_provider,profiles,configured,base_url,model,label,result,connection_test},errors:[]
}
```

公开calendar.dates只显示最近15个交易日，不能当作筛选/均线所需完整日历。jobs以prepare/review/evidence/llm/llm_test/stock/watchlist/trends为键，值为`{status:'running'|'done'|'error',message}`。job完成不代表日线已生成：保护时段analysis.status可以为deferred。api冷却为本机已知的剩余等待，不代表外部服务保证恢复时间。

竞价rows含`rank,thscode,name,score,factors,quality,auction_pct,auction_amount,updated_at,phase,sources`；公开状态去掉raw和大体积历史，详细本机轨迹另取。

个股analysis含`thscode,ticker,name,metadata,date,status,trend,trend_score,trend_factors,trend_coverage,history,warnings,context,score_method,source`。service附加`auction:{row,history,date,mode,tracking,scope_count}`。analysis.date与auction.date分别是日线截止日/竞价会话日。watchlist行含`thscode,name,sources,tracking`。

趋势池含`date,status,rows,candidate_count,candidate_available_count,evaluated_count,valid_history_count,selected_count,matched_count,coverage,warnings,generated_at,completed_at,method,thresholds`。rows含`rank,thscode,name,date,score,trend_score,trend,reason,source`；score与trend_score同值。trend附`score_factors,score_weights,score_factor_coverage_pct`，覆盖按权重计算。coverage保留池日数、历史失败、短历史、未评估、有限样本和取消保护标记。

默认publicConfig见config.py：poll_seconds=3、batch_size=100、universe='focus'、watchlist=[]；七因子权重gap=.15、amount=.15、turnover=.10、volume_ratio=.10、late_momentum=.20、retention=.15、continuity=.15。缺因子不补零。

## 报告版本、AI附录与两期比较（1.3）

`review_id`是报告公开证据的规范JSON SHA-256，递归排除raw、敏感键及导出/AI/对比元数据；包含generated_at/completed_at等来源时间。相同日期重新生成不是同一版本。Service按报告对象身份缓存此值：**报告对象发布后不可原地修改**，只能构建新对象再替换；否则缓存会失效。保存与专属AI请求应同时提交当前日期、provider和review_id，版本不匹配立即拒绝。

`review`是用户当前查看的报告；内部`latest_review`是自动复盘与趋势刷新所依据的最新实盘报告。读取历史只切换review并设置viewing_archive，不能改变latest_review。自动复盘更新latest_review和存储，但用户查看历史时不强行跳转；手动生成完成会切换至新报告。自动任务不能因历史视图日期较旧就重复生成当日报告。

AI结果为`{text,generated_at,mode,provider,label,model,scope,review_date,review_id}`。`scope='market'`可含当前竞价和个股摘要，不可作为某天报告附录；`scope='review'`只发送指定报告白名单摘要，竞价为空且phase=not_included，个股分析为空。提交时固定服务商/配置/报告版本。按`data/ai-reviews/{live|demo}/YYYY-MM-DD-{provider}.json`原子归档，只写结果白名单，不包含配置或凭据；读取上限1,000,000字节。恢复或附录必须同时匹配scope、mode、review_date、review_id、provider。用户切换历史视图后，旧在途报告AI可保留其独立归档，但不得显示到另一报告下；模式改变则丢弃结果。同日同服务商归档保存最近一次结果，版本不匹配不得沿用。

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

矩阵至多10个目标日前交易日，完整池缺失或代码重复/无效时计数为null。`buckets`是字符串键且互斥；无法确认的高度单列unknown，窗口下限另计，1板下限不等于首板。封单分布剔除当前封单额大于峰值的异常；`above_100_count`独立于其他数值异常计数。原因保留完整原文，最多12组、每组最多10个代码，不解释为行业。

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

取消、全部不可用或持久化前版本冲突时原报告保留；原有ready补充不被新partial替换。可用补充通过完整报告深拷贝写入`official_context`、`sentiment`、`enriched_at`，不改变原复盘`status`。更新的是报告版本，不是盘前选股或竞价排名；旧AI附录随review_id变化失效。自动复盘只生成本机情绪结构，不调用这四个补充分项。

提交前预检Markdown渲染；保存证据并同步内存版本、清除旧AI后才写Markdown文件。文件被占用等写入失败不回滚已保存证据，会提示可直接重试本机导出；不能声称JSON、SQLite、Markdown整体构成原子事务。

Markdown包含情绪结构、已补充观察及缺失口径。LLM经`build_summary`导出`sentiment`和`official_observations`显式白名单，不发送raw；每榜每区间进一步限10条，保留真实总数与截取提示。原有DeepSeek/OpenAI/custom服务商隔离、版本绑定和用户主动发送规则继续适用。

`HiThinkProvider.rate_limit_status()->{rate_limited,cooldown_seconds}`只读本机状态；`APIError.retry_after_seconds`是安全数值元信息。同进程同Key的实例共享429/4001冷却，冷却中请求快速返回受控错误。常规重试尊重可解析的Retry-After；要求等待超过15秒时不截短后立即重试，而交给调用方稍后处理。竞价仍每批单次HTTP尝试。该兼容机制不表示官网保证提供Retry-After，也不保证特定吞吐或恢复时效。

## 持久化与维护

- SQLite按live/demo隔离批次及报告；JSON复盘保留raw。公开状态递归去掉raw，不代表原始证据丢弃。
- `data/stock-metadata.json`缓存官方解析；`data/trend-pool.json`缓存筛选；`data/stocks/完整代码-日期.json`保存个股日线分析。
- `_complete_trend_cache`允许复用因日期推断或短历史而partial、但全部候选处理完且池齐全/无请求失败/无保护中断的同日缓存。中断或失败不能冒充完成；最新复盘完成后仍主动重筛。
- 演示拒绝真实查询/自选变更/趋势刷新，不能将真实个股或趋势池显示为演示信号。
- 原生中文前端无CDN；保持查询/加入关注分开，显示日线日期、竞价日期、模式、质量及评分口径。
- unittest使用自包含夹具、临时目录；真实认证、真实交易时段和离线边界测试的验收分别记录。修改同步README、策略、数据源、架构、AGENTS及本契约。

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
