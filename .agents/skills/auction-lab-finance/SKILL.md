---
name: auction-lab-finance
description: 维护竞价研究台中的同花顺 Financial API 接入、实时竞价采集、个股和板块查询、收盘核验及持续优化接口。修改本仓库的 provider、金融配置端点、数据口径、竞价证据链或相关文档测试时使用；普通用户运行程序或泛化金融查询不使用。
---

# 竞价研究台同花顺维护

这个项目级 Skill 把官方 `hithink-finance` 能力约束到竞价研究台的实现和证据规则。它只指导维护工作，不参与网页运行，不持有凭据，也不替代本项目的 HTTP provider。

## 接手与路由

1. 先读取项目根目录 [AGENTS.md](../../../AGENTS.md) 和 [AI 修改与维护手册](../../../docs/AI_MAINTENANCE.md)。它们的项目约束优先于通用 Skill。
2. 修改金融接入时读取 [同花顺接入指南](../../../docs/FINANCE_CONNECTION.md)、[本机接口说明](../../../docs/API_REFERENCE.md) 和 [数据口径](../../../docs/DATA_SOURCES.md)。
3. 修改竞价评分、每日标签或参数研究时再读取 [持续优化手册](../../../docs/CONTINUOUS_OPTIMIZATION.md)、[历史回放指南](../../../docs/BACKTEST.md) 和 [策略说明](../../../docs/STRATEGY.md)。
4. 核对实际源码和测试。主要入口是 `app/provider.py`、`app/service.py`、`app/server.py`、`app/storage.py`、`app/research.py` 与 `app/daily_validation.py`；不能只按文档猜测现有行为。
5. 需要核对上游接口字段时，如果当前 Agent 已提供全局 `hithink-finance` Skill，只读取与本次端点对应的入口和 reference。它不可用时使用项目中列出的官网链接；不要因此自动安装或更新工具。

## 运行边界

- 应用运行时只通过 `app/provider.py` 调用官方 Financial API，通过 `/api/finance/*` 和项目外的用户级凭据完成配置与验证。本 Skill 不是 Python 依赖、浏览器插件或数据代理。
- 不把通用 `API_KEY`、`FUYAO_TOKEN`、其他 Agent 的密钥库或模型 Key 当作本项目金融授权。不得读取、复制、显示或写入真实 Key；测试使用临时凭据和模拟网络。
- 不静默更新全局 Skill，不安装 CLI、MCP、Python SDK，不初始化 DuckDB 或下载全市场数据。只有用户明确要求对应环境变更时才执行。
- 实时竞价必须逐批执行“接收 → 原始落盘 → 计算 → 发布”，不能等待十分钟结束后统一分析，也不能把 LLM 放进采集关键路径。
- 官方成功同时要求 HTTP 200 与业务 `code == 0`。失败、`null`、未知、空池和真实 0 必须保持区分；股票代码按字符串处理并通过官方搜索精确消歧。
- 收盘数据只能作为当日竞价结果标签，不能反向进入当天候选、因子或排名。外部权重建议只归档，不能自动调用 `/api/config`。

## 修改对应关系

| 改动 | 至少核对 |
| --- | --- |
| 官方端点、参数、字段或分页 | `app/provider.py`、`docs/DATA_SOURCES.md`、接口测试 |
| Key 保存、状态或连接验证 | `app/config.py`、`app/service.py`、`app/server.py`、前端接入页、`docs/FINANCE_CONNECTION.md` |
| 竞价批次、因子或排名 | `app/engine.py`、`app/storage.py`、`docs/STRATEGY.md`、竞价和服务测试 |
| 个股或板块查询 | 对应纯计算模块、service 调度、日期与覆盖字段、AI 白名单、HTTP 测试 |
| 每日核验或调参 | `app/research.py`、`app/optimization.py`、`app/daily_validation.py`、防未来数据测试 |
| 本机 HTTP 契约 | `app/server.py`、`CONTRACT.md`、`docs/API_REFERENCE.md`、路由覆盖测试 |

新增官方能力前先确认账户授权、日期语义、单位、分页、限流和是否公开；无法确认的字段保持缺失。当前成分不能伪装历史成分，成交额不能描述为资金净流入，接口响应组装时间不能描述为交易所逐笔时间。

## 验证与交付

- 先运行受影响模块测试，再运行 `python -m unittest discover -s tests -q`。前端有改动时检查两个 JavaScript 文件；Skill 本身有改动时运行 Skill Creator 的 `quick_validate.py`。
- 所有自动测试使用临时目录，不覆盖 `data/`、实盘 SQLite、用户报告或凭据。
- 分别报告离线测试、官方认证、真实交易日十分钟采集和付费模型生成；其中一项通过不能代替其余项目。
- 同步修改 README、契约、专项文档和验收记录；运行 `python tools/build_release.py` 并确认分发包不包含数据、凭据或用户级全局 Skill。
- 保留已有用户改动，使用明确文件列表提交；不得通过重置、清库或伪造数据获得通过结果。
