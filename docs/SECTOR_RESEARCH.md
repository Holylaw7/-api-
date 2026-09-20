# 指定板块取数与 AI 研判 · 1.6

程序位于 `E:\A股竞价`。本功能让用户直接选择同花顺官方行业或概念，读取成员与目标日表现，再交给所选 AI 分析。它解决原盘面摘要只展示前 30 个板块、无法为指定方向补充同行证据的问题。

板块数据由主系统调用金融接口，金融 Key 留在本机服务端。AI 只收到经过白名单处理的证据；不获得 Key，也不能执行任意金融接口请求。无需 AI Key 就能搜索、取数和下载 JSON。

## 使用步骤

1. 双击 `启动系统.cmd`，进入「收盘复盘」，生成或读取需要研究的真实报告。确认页面显示的日期；当日报告须在 15:10 后进行板块取数。
2. 在「AI 辅助研判 → 联网查询指定板块」输入名称，如 `PCB`，或官方目录中的完整指数代码，点击「搜索板块」。从返回的官方结果中选择，最多 3 个。
3. 点击「只读取板块数据」。完成后查看指数趋势、同行涨跌分布、成交额、当前成分中目标日涨停及连板情况，以及覆盖率和缺失提示。此操作不调用模型。
4. 需要文字解释时，选择 DeepSeek、ChatGPT / OpenAI 或自定义服务，填写问题，再点击「联网取数并 AI 分析」。系统会为本次选择重新取数、保存证据，然后调用一次所选模型。只读取过数据并不会自动生成或更新 AI 回答。
5. 点击「保存板块证据 JSON ↓」下载本次证据。成员明细受下文样本上限约束，证据文件不代表全板块逐股历史数据。
6. 保存文字研判时，在报告顶部勾选「附上当前 AI 研判」，点击「保存 Markdown ↓」。附录会记录问题、板块代码、证据编号及取数时间；详细证据另存 JSON。

「刷新已保存数据 ↻」只读取本机留存证据，不重新请求金融接口。修改提问、选择其他板块或更新预览后，旧回答仍属于原问题及原证据，页面会提示差异；需要重新分析才会产生新的回答。

搜索采用官方行业、概念全目录的名称子串匹配，英文大小写不敏感；完整代码精确匹配。2026-09-20 的授权接口验证已确认 PCB 对应 `885959.TI`、MLCC 对应 `886112.TI`，可以直接输入这些代码，后续仍会通过官方目录核验。其他词未找到时会明确提示，不自动把一个相关股票、涨停原因或另一概念冒充该板块。

## 日期、组成与指标含义

目标日期必须通过本次官方交易日历核验，且已收盘；当日须达到上海时间 15:10。这是本系统的研究门槛，不是上游承诺永不修订的时刻。

| 数据 | 处理方式 | 不能据此推断 |
| --- | --- | --- |
| 板块成员 | 读取接口当前成员，`members_as_of` 记录本机取回时间，`membership_basis=current_members_view` | 当年或目标历史日的真实成分组成；成员调整的生效时间 |
| 指数趋势 | 每板读取截至目标日的 60 自然日日线；逐日核验，指数没有复权语义 | 缺失日均线、未来走势或涨停概率 |
| 同行行情 | 优先复用原报告日期合格的行情；最近收盘日缺少的样本再取快照，更早日期只读取有限历史日线 | 用当前快照替代旧日行情；未取得行情就是下跌或零成交额 |
| 休市日快照 | 仅在既有严格条件及官方日历支持下，明确标为 `provisional` 的最近收盘日推断 | 已独立核实每只股票的真实行情日期 |
| 涨停、连板聚集 | 当前有效成员与报告同日完整收盘涨停池相交；严格连板使用报告已核实的 `consecutive_days` | 当前组成等于历史组成；`5天4板` 等于 4 连板 |
| 成交额 | 仅加总取得的有效成员样本，保留有效数量；元为单位 | 主力净流入；不同概念重叠成员的金额可以直接相加 |

同行平均涨幅、中位数与上涨/下跌/平盘家数只使用已取得的有效涨幅。涨停聚集使用完整有效成员与完整目标日池的交集，分母与行情样本不同；不要用展示出来的 20 或 30 行反推整体数量。成员有异常或重复代码时保留不完整提示，不宣称全板块已核验。

`consecutive_known_count` 是涨停成员中严格连板数已知的数量；部分已知时，连板数量只覆盖已知记录，全部未知则为 `null`。未取得完整涨停池、盘中池或日期不合格时，涨停标签保持未知，不能当零。净资金字段继续为 `net_flow=null`、`net_flow_status=unavailable`。

## 请求与展示上限

| 范围 | 上限与规则 |
| --- | --- |
| 目录 | 行业、概念各读取一次完整目录；同一主进程缓存 15 分钟。官网还有区域、特色目录，本功能未启用 |
| 每次板块选择 | 1—3 个去重后的完整官方代码，证券与指数代码均保留字符串 |
| 最新同行报价 | 合并当前成员后按代码顺序选择，最多 300 个去重代码，每批最多 100；原报告合格行情优先复用 |
| 历史同行日线 | 每板按代码顺序最多 20 个候选，合并去重后最多 60 次短日线请求；已有合格行情可减少请求 |
| 构建业务调用 | 最近收盘日最多 10 次：日历 1、成员 3、指数日线 3、报价 3；更早日期最多 67 次：1+3+3+60。目录缓存未命中另加 2 次 |
| JSON 每板明细 | 同行最多 100 条，涨停成员最多 30 条；保留全量计算所得统计及 `shown_count/truncated` |
| AI 每板明细 | 同行最多 30 条，涨停成员最多 30 条；3 个所选板块均独立放入 `targeted_sectors`，不再受普通板块榜前 30 名排除 |
| 页面每板明细 | 同行与涨停成员各最多 20 条；更多已取得明细看 JSON |

这些是应用设定的取数和展示边界，不是官网对普通行情显式代码请求承诺的硬上限。业务调用数不包含 provider 的网络重试；限流、缓存、成员重叠和缺失会使实际请求数与等待时间不同。`coverage.business_calls` 记录构建器内调用，主服务已单独读取或缓存的目录不计入这个数。

## 本机接口

主系统地址是 `http://127.0.0.1:8765`。POST 使用 JSON，并带 `X-Local-App: auction-lab`；浏览器必须同源。本机接口不接受金融 Key 或任意远程 URL 作为板块请求参数。

| 方法与路径 | 入参与作用 |
| --- | --- |
| POST `/api/sectors/search` | `{"query":"PCB"}`；1—80 字名称或完整代码。返回 `ok/query/matches/exact/unmatched_note`，每个匹配含 `thscode/name/category` |
| POST `/api/sectors/research` | `{codes:[完整代码],date:报告日期,review_id:报告版本}`；开始异步只取数，返回 `ok/message/started`，任务状态为 `jobs.sectors` |
| GET `/api/sectors/research?date=...&review_id=...` | 查询对应有效报告版本最近一次本机板块证据；未运行返回 `status=not_run`，不联网 |
| GET `/api/sectors/export?evidence_id=...` | 校验并下载该编号证据 JSON，可用于审计旧证据；不接受文件路径 |
| POST `/api/llm` | 追加 `fetch_sectors:true, sector_codes:[完整代码]`，并提供 `review_date/review_id`；先取数再生成对应报告的 AI 分析，任务为 `jobs.llm` |

从 GET `/api/state` 取得 `review.date` 和 `review_id`，从搜索结果取得 `thscode`，不要猜测板块后缀或把纯六位代码直接提交。下列 JSON 是字段模板，其中占位内容须替换为页面取得的实际值：

```json
{
  "question": "对比所选板块的同行表现与涨停聚集，列出覆盖和缺失。",
  "provider": "deepseek",
  "review_date": "YYYY-MM-DD",
  "review_id": "报告的64位小写十六进制版本",
  "fetch_sectors": true,
  "sector_codes": ["885959.TI", "886112.TI"]
}
```

`fetch_sectors` 必须是 JSON 布尔值；提供 `sector_codes` 时必须为 `true`。不带这两个字段则继续原有摘要分析。服务商配置在提交时固定；界面切换只影响后续请求。

## 证据结构与保存

```text
{
 date, mode:'live', review_id, evidence_id, generated_at, status,
 coverage:{business_calls,catalog_calls,calendar_calls,member_calls,
           index_history_calls,stock_quote_calls,stock_history_calls,
           quote_code_count,historical_code_count},
 boards:[{
   thscode,name,category,status,membership_basis,members_as_of,member_count,
   quote_scope:{source,selection,max_stock_quotes,max_historical_per_board},
   coverage:{membership_complete,requested_count,quoted_count,quote_coverage_pct,
             shown_count,truncated,limit_pool_complete,limit_up_shown_count,limit_up_truncated},
   price_trend, statistics:{total_members,quoted_count,valid_change_count,
      advancing,declining,unchanged,mean_change_pct,median_change_pct,
      turnover_sum,turnover_known_count,limit_up_count,consecutive_count,consecutive_known_count},
   members,limit_up_members,net_flow:null,net_flow_status:'unavailable',warnings
 }], warnings,definition
}
```

证据文件位于 `data/sector-research/evidence/<evidence_id>.json`；`latest/<date>-<review_id>.json` 只记录该报告版本的最近证据指针。`evidence_id` 是完整证据（排除自身编号）的规范 JSON SHA-256 前 24 位十六进制，每次取数时间也参与计算。读取重新校验摘要，并限制 8 MiB。编号用于内容识别，不是授权令牌。

板块证据与基报告独立保存，**不改变基报告 `review_id`**，不追加进原报告对象，也不改竞价因子、关注池或实时排名。旧证据按编号保留；报告重新生成后，新版本不会自动沿用旧板块预览。已有证据编号仍可单独导出审计。

本次 AI 结果另记 `question`（最多 2000 字符）、`sector_codes`、`sector_evidence_id`、`sector_generated_at`，与原有 `scope='review'/review_date/review_id/mode/provider` 一起归档。Markdown 附录保留这些来源信息，正文及来源文本均作转义；详细板块数据仍以独立 JSON 为准。

## 调度、失败与复用

- 09:10–09:26（含整分钟）拒绝新的板块联网搜索、取数和取数后 AI 任务。进行中的取数在每个业务请求前后检查保护、关闭和模式变化；不将板块任务放入竞价每批处理过程。已有本机证据仍可浏览。
- 没有所选模型 Key 时，可只取数；组合任务会在取数前提示配置 AI。目录、交易日或版本校验失败时不调用模型。
- 所有板块都没有有效行情、指数目标日收盘或有效成员与完整收盘涨停池交集时，保存 `unavailable` 证据并停止，不发起付费 AI 生成。部分证据可用则为 `partial`，模型须同时看到缺失；有效的零涨停交集不算缺失。
- 取数完成、模型调用前再次核验报告版本。用户切换历史视图后，旧结果只保留相应档案，不展示到另一天报告下；没有自动重试付费生成或失败后切换供应商。
- 独立助手（8766）保持原有可选本机摘要流程，不因聊天或加载摘要隐式启动金融取数。这一组合能力位于主系统（8765）；外部 AI 可以在用户授权范围调用上述本机接口或读取已下载 JSON。

实现入口：`app/sector_research.py` 负责有界构建，`app/sector_library.py` 负责证据保存与校验，`Service.run_sector_research/run_llm` 负责调度绑定，`llm._targeted_sector_summary` 负责独立显式白名单。新增字段不能直接把原始响应、配置或凭据送入模型。测试须使用临时目录和模拟接口；离线通过、真实金融接口验证与真实模型生成验证分开记录于 [VALIDATION.md](VALIDATION.md)。

## 官方依据

契约核对以 [官方全文指南](https://fuyao.aicubes.cn/llms-full.txt)、[指数目录、成分与日线](https://fuyao.aicubes.cn/docs/api-reference/a-share-index/)、[股票行情字段](https://fuyao.aicubes.cn/docs/api-reference/prices/) 为准。官方指数成员没有历史日期参数，快照没有指定历史日的能力；目录名称的存在必须以实际官方返回确认。

[主力资金说明](https://fuyao.aicubes.cn/docs/api-reference/capital-flow/) 当前标明未开放外部接入，因此本版不请求这些计划接口，也不从成交额生成净资金流。后续接入须重新核验开放状态、授权和定义。
