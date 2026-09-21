# 同花顺金融 API 接入与开源部署 · 1.7

## 第一次使用

1. 从本项目 GitHub 下载源码 ZIP 并解压，或克隆仓库。项目可位于任意可写文件夹，不依赖维护者的 E 盘、开发缓存或账户。安装 Python 3.10+，不需要另外安装 Python 库、Node.js 或数据库服务。
2. Windows 双击「启动系统.cmd」。macOS/Linux 在项目目录运行 `sh start.sh`，或直接运行 `python3 run.py` 并打开 `http://127.0.0.1:8765`。
3. 在网页左侧点击常驻入口「同花顺接入」。新电脑没有 Key 时，也会显示首次接入提示。
4. 点击官方「申请 / 管理 API Key」链接，进入 [同花顺 API Key 管理](https://fuyao.aicubes.cn/admin)，按官网指引使用自己的同花顺账户登录并创建 Key。详见 [官方快速开始](https://fuyao.aicubes.cn/docs/quickstart/)。账户权限以官方签发和授权为准，开源项目不附赠 API 访问权。
5. 将 Key 粘贴到本机网页的密码输入框，点击「保存同花顺 Key」。保存成功后输入框清空；以后仅显示配置来源和状态，不回显密钥。
6. 点击「测试同花顺连接」，查看认证及日历读取结果。输入框有尚未保存的改动时，先保存再测试，避免误以为测试了新 Key。
7. 按需前往「实时竞价」启动监测，或进行股票查询、收盘复盘及指定板块研究。保存和测试不会替你启动采集。以后正常重新启动且已有 Key 时，原一键启动入口仍会启动监测；如仅想查看页面，可运行 `python run.py --no-auto-start`。

同花顺金融 Key 用于取得行情、日历、涨停池等结构化数据。DeepSeek / OpenAI Key 仅用于文字解释，在「策略与接入」配置。只提供金融 Key 也能使用确定性评分和复盘；不需要购买或配置模型才能取数。

## 项目级维护 Skill

源码包内置 [项目级 `auction-lab-finance` Skill](../.agents/skills/auction-lab-finance/SKILL.md)。支持 Agent Skills 的维护工具可以直接发现它，用于理解本项目的 provider、金融配置端点、竞价证据链和验证要求。它只是一份维护指令，不被网页或 Python 服务调用。

需要更完整的官方接口目录时，维护 AI 或开发者可以另行安装官方仓库的全局 `hithink-finance` Skill：

```powershell
npx skills add HiThink-Tech/Financial-API --skill hithink-finance -g --yes
```

普通用户运行本项目不需要 Node.js、`npx` 或另行安装 Skill。两种 Skill 都不是程序插件，应用运行时不会调用它们；真实行情仍由 `app/provider.py` 通过官方 Financial API 读取，并由本网页的 `/api/finance/*` 接口配置和验证。发现或安装 Skill 不会提供、保存或验证 API Key。

名称也要区分：项目级 Skill 位于源码的 `.agents/skills/auction-lab-finance`，官方 Skill 通常安装在 Agent 的用户级 Skills 目录；下文 `%APPDATA%\hithink-finance` 或其他平台对应目录是本项目保存金融凭据的用户级配置命名空间。三者不是同一个目录，也不能互相替代。

## 网页上可以看到什么

- 固定的官方接口地址 `https://fuyao.aicubes.cn`、申请入口与使用指南。
- 是否已配置、是否保存在本机、当前凭据来源；不显示 Key、尾号或可用于识别它的摘要。
- 未测试、正在测试、测试通过或失败，以及上次检查时间、耗时、有效交易日期数量。
- 保存与测试为何暂不可用的具体说明，例如监测/任务正在运行、演示模式或09:10–09:26竞价保护时段。

**已保存不等于已验证。** 状态查询和页面刷新只检查本机配置，不为了显示绿色状态自动请求官方接口。测试使用 `GET /api/a-share/calendar/trading-days`，一次业务请求、不自动重试；不调用模型、不扫描股票、不生成报告。通过只证明这次认证和日历读取正常，其他数据权限、更新速度及额度须以各自接口实际结果为准。

主力资金流等尚未公开的能力不会因保存 Key 自动开放；成交额不会被当作净流入。板块研究的历史成员、日期及覆盖限制见 [指定板块指南](SECTOR_RESEARCH.md)。

## 密钥存储与迁移

| 系统 | 用户级凭据文件 |
| --- | --- |
| Windows | `%APPDATA%\hithink-finance\credentials.env` |
| macOS/Linux | `${XDG_CONFIG_HOME:-~/.config}/hithink-finance/credentials.env` |

文件位于项目外，格式沿用 `HITHINK_FINANCE_API_KEY=...`。应用使用原子替换保存；非 Windows 系统将凭据文件权限设为仅当前用户可读写。它是当前用户的本机凭据文件，不是加密保险库。

读取顺序为：网页保存的用户级文件 → 当前进程的 `HITHINK_FINANCE_API_KEY` → Windows 用户环境变量。这样网页更新后重新启动不会被旧环境值替换。没有用户文件时仍可用环境变量部署；程序不修改 Windows 用户的全局环境变量。

多个项目副本在同一个操作系统用户下会共用此用户凭据目录。已有进程应停止后重新启动，以一致加载新的配置。复制源码到另一台电脑不会复制密钥，需要在新电脑自行配置；无需把个人 Key 写进代码、README、示例、GitHub 或打包文件。

应用仅监听 `127.0.0.1`，这是个人本机研究台，不是带账户隔离的公共托管服务。部署页面不需要开放到公网；其他开源使用者在自己的电脑运行即可。

## 本机接入接口

这些端点由本项目提供，供网页或本机集成使用；金融数据本身仍走官方接口和 `X-api-key` 请求头。官方地址固定，不接受任意代理地址，避免将已保存 Key 转发到其他服务。

| 方法与路径 | 请求 / 用途 |
| --- | --- |
| `GET /api/finance/status` | 读取无密钥的配置与验证状态，不联网 |
| `POST /api/finance/config` | JSON `{ "api_key": "你的同花顺 API Key" }`；只保存，不调用官方、不自动采集 |
| `POST /api/finance/test` | JSON `{}`；使用已保存或环境提供的当前 Key，后台任务 `finance_test` |
| `GET /api/state` 或 `/api/events` | `finance` 字段同步上述状态和测试结果 |

所有 POST 必须带 `Content-Type: application/json`、`X-Local-App: auction-lab`，并符合本机 Host 与同源检查。不要把 Key 放进 URL。旧 `POST /api/credentials` 为兼容旧客户端，仍保留保存并启动的语义；新网页使用 `/api/finance/config`。

保存需要先停止监测并等候后台任务完成，以免一轮数据读取途中换 Key；演示和竞价保护时段拒绝保存/测试。更换 Key 会清除原测试状态与相关接入缓存，后续重新测试。失败结果只显示整理后的原因，不回显请求体、认证头或上游原始错误正文。

先体验演示的新用户，可点击接入页的「退出演示，配置同花顺」。它使用 `POST /api/demo/exit {}`，仅取消合成回放并恢复停止的实盘视图，不要求已有Key、不联网、不启动采集。退出后即可保存自己的Key。

## 故障处理

| 情况 | 处理 |
| --- | --- |
| 未安装 Python / 双击后提示找不到 Python | 安装 Python 3.10+，确认 `py -3` 或 `python` 可运行；macOS/Linux 使用 `python3` |
| 已保存但显示未测试 | 点击测试；保存不会伪造认证结果 |
| 认证失败 / 2001 | 在官方管理页核对或重新签发 Key，保存后再测试 |
| 权限不足 / 2003 | 向官方确认当前 Key 对相应数据能力的权限 |
| 限流 / 4001 / 429 | 等待冷却后手动重试，避免频繁点击 |
| 网络失败或超时 | 检查网络与官方站点可达性；不用其他网站代收金融 Key |
| 保存按钮暂不可用 | 按页面提示停止监测、等候任务完成，或避开09:10–09:26 |
| 提示服务版本过旧 | 刷新页面不会更新 Python 后台；关闭旧服务或重启电脑，再双击新版目录的启动文件 |
| 双击后窗口立即关闭或出现 `evel is not recognized` | 使用最新版 `启动系统.cmd`；源码中的批处理应为纯 ASCII + CRLF，中文提示由 Python 或网页显示 |
| 8765 被其他程序占用 | 使用 `python run.py --port 8768 --no-auto-start`，再打开对应本机端口；不要覆盖其他应用 |

本轮接口、存储、未配置启动及真实连接验收分别记录在 [VALIDATION.md](VALIDATION.md)。维护者先读 [AGENTS.md](../AGENTS.md) 与 [CONTRACT.md](../CONTRACT.md)；连接测试必须保持有界，不进入竞价逐批路径。
