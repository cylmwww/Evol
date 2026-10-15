# evol

**面向 Pull Request 的多智能体代码审查工作台。** evol 接收代码变更，组织多个审查角色分析风险，输出带代码定位、证据、修复建议和测试建议的报告，并将确认后的反馈用于后续审查与规则演进。

项目围绕一条完整链路组织：**提交变更 → 协作审查 → 证据校验 → 查看报告 → 反馈与改进**。可以通过 Web 控制台手动提交 Unified Diff，也可以通过 API 或 GitHub Webhook 接入现有流程。

## 主要功能

| 功能 | 说明 |
|---|---|
| 多 Agent 协作 | Lead 分配任务，Security 与 Correctness/Reliability Worker 分别分析，Critic 复核候选问题，Lead 汇总结论 |
| 证据与质量门禁 | 校验新增行定位、必要字段、证据引用和置信度；高风险问题还需要修复与测试建议 |
| 任务管理 | 保存状态、执行轨迹和报告，支持异步执行、取消以及从持久化 checkpoint 续跑 |
| Web 控制台 | 提供运行总览、发起审查、任务中心、Skills 和演进实验室 |
| GitHub 集成 | 接收 PR 事件，下载 Diff，创建异步任务；可选回写或更新 PR 评论 |
| 修复补丁 | 模型生成 Unified Patch，通过校验和配置的仓库测试后，创建修复分支和 Draft PR |
| 反馈与记忆 | 记录误报、漏报和不良修复反馈；确认后的经验按租户与仓库范围检索，用于后续审查 |
| 提示词与 Skill 演进 | 从失败案例生成候选版本，通过验证集、留出集回放及门禁后激活，支持版本回滚 |
| 存储与队列 | 默认使用 SQLite 与进程内队列；可配置 PostgreSQL 和 Redis Streams |
| 访问管理与观测 | 用户登录、角色权限、租户与仓库访问控制、管理审计，以及 Prometheus 指标和 OpenTelemetry |

审查使用 `agentic` 模式，需要配置模型。未配置模型时可以启动服务、打开控制台和访问健康检查，但不能提交审查任务。模型结论经过门禁筛选，仍需结合项目上下文人工判断。

## 架构与执行流程

后端使用 Python，HTTP 服务基于标准库 `ThreadingHTTPServer`；前端由静态 HTML、CSS 和 JavaScript 构成，由同一服务提供页面与 API，无需单独构建前端。

```text
Web 控制台 / HTTP API / GitHub Webhook
                    │
                    ▼
              ReviewService
                    │
       同步执行 / 异步任务队列
                    │
                    ▼
       ReviewHarness + AgentRuntime
       解析 Diff → 执行审查 → 生成报告
                    │
                    ▼
              AgenticReviewer
       Lead 分配任务并选择领域 Skill
                    │
           ┌────────┴─────────┐
           ▼                  ▼
     Security Worker   Correctness/Reliability Worker
           └────────┬─────────┘
                    ▼
              Critic 复核
                    ▼
              Lead 最终综合
                    ▼
          FindingGate → JSON / Markdown 报告

共享能力：仓库取证工具、上下文管理、反馈记忆、执行预算与轨迹
持久化：  TaskStore（SQLite / PostgreSQL），保存状态、checkpoint 与报告
```

| 角色 | 职责 |
|---|---|
| Lead | 判断风险、分配领域任务、选择 Skill，并综合最终结果 |
| Security Worker | 检查输入、权限、敏感数据和危险调用链等安全问题 |
| Correctness/Reliability Worker | 检查逻辑、状态、异常、并发、资源管理与兼容性问题 |
| Critic | 对候选问题提出反例，检查证据和误报风险 |

普通任务采用单轮协作；高风险任务允许 Worker 在受限步数内使用工具取证，Lead 最多请求一轮返工。角色执行受 Token、时间和上下文预算约束。

工具层提供 Diff 搜索、仓库文件读取、符号分析、AST 分析及扫描器调用等能力。仓库级取证需要提供服务端可访问的本地检出目录；仅提交 Diff 时，可用上下文主要来自变更本身。工具与扫描器提供事实，模型进行分析，确定性门禁检查结果是否满足输出条件。

## 代码目录与入口

```text
evol/
├── __main__.py          # python -m evol 启动入口
├── api.py               # HTTP 路由、认证入口、静态页面服务
├── service.py           # 组装组件，协调审查、反馈、修复与演进
├── harness.py           # 审查阶段编排、状态流转与 checkpoint
├── runtime.py           # 节点执行、工具注册与参数校验、预算和取消
├── agentic_core.py      # Lead / Worker / Critic 协作协议
├── repository_tools.py  # 仓库与 Diff 取证工具
├── context_manager.py   # Diff 压缩、上下文预算与 Observation 整理
├── gates.py             # 问题定位、证据、置信度与输出门禁
├── skills.py            # Agent Skill 加载、元数据与资源管理
├── memory.py            # 工作记忆与确认反馈的检索
├── evolution*.py        # 提示词候选生成、回放评测与版本管理
├── skill_evolution.py   # SKILL.md 候选包评测与版本管理
├── patching.py          # 模型补丁生成、校验与 Draft PR 创建
├── verifier.py          # Python 语法与仓库测试验证
├── github.py            # GitHub API、签名及 App 认证
├── store.py             # SQLite 持久化
├── postgres_store.py    # PostgreSQL 持久化与存储选择
├── task_queue.py        # 进程内 / Redis 队列、重试与死信处理
├── auth.py              # 登录与角色权限
├── observability.py     # Trace 与告警
├── telemetry.py         # 模型和工具调用账本
├── metrics.py           # Prometheus 指标
├── report.py            # Markdown 报告生成
└── config.py            # 环境变量与 .env 配置读取
web/                     # Web 控制台页面与静态资源
skills/                  # 按审查领域组织的 Agent Skill 包
requirements.txt         # Python 运行依赖
.env.example             # 配置项与默认值示例
Dockerfile               # 应用容器镜像
docker-compose.yml       # 应用 + PostgreSQL + Redis 组合配置
```

理解代码时，可以从 `__main__.py → api.py → service.py` 查看请求如何进入系统，再沿着 `harness.py → agentic_core.py → gates.py` 查看审查如何完成。

## 快速开始

以 Python 3.11 为运行基准。下面的命令在项目根目录的 PowerShell 中执行。

### 1. 安装依赖

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 2. 配置模型与登录

在同一个 PowerShell 窗口中配置一个兼容 OpenAI Chat Completions 的模型端点，以及首次登录的管理员账户：

```powershell
$env:EVOL_LLM_PROVIDER = 'custom'
$env:EVOL_LLM_BASE_URL = 'https://<你的模型服务地址>/v1'
$env:EVOL_LLM_API_KEY = '<你的 API Key>'
$env:EVOL_LLM_MODEL = '<可用的模型名称>'

$authBytes = New-Object byte[] 32
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($authBytes)
$env:EVOL_AUTH_REQUIRED = 'true'
$env:EVOL_AUTH_SECRET = [Convert]::ToBase64String($authBytes)
$env:EVOL_BOOTSTRAP_ADMIN_USERNAME = 'admin'
$env:EVOL_BOOTSTRAP_ADMIN_PASSWORD = '<替换为至少 10 个字符的密码>'
```

请替换所有占位值。环境变量只对当前窗口及其子进程生效。Bootstrap 管理员只在用户名不存在时创建，重启不会覆盖已有用户密码。

也可以将配置写入根目录 `.env`，配置项见 [`.env.example`](.env.example)。启动时会自动读取该文件，进程环境变量优先；修改后需要重启服务。`.env` 已被 Git 忽略，密钥不应写入公开文件。

除 `custom` 外，代码还提供 `deepseek`、`openrouter-deepseek-free` 和 `openrouter-free` 预设。使用预设时需配置对应服务的密钥；具体模型是否可用以模型服务商为准，可通过 `EVOL_LLM_MODEL` 指定模型。

### 3. 启动并提交审查

```powershell
.\.venv\Scripts\python.exe -m evol
```

打开 [Web 控制台](http://127.0.0.1:8080/)，使用上一步设置的管理员账户登录。在“发起审查”中填写 `owner/repository` 格式的仓库名称、可选 PR 编号和 Unified Diff，提交后进入“任务中心”查看状态与报告。

健康检查入口为 [`/health`](http://127.0.0.1:8080/health)，返回服务状态、模型配置和队列类型。默认数据库为当前工作目录下的 `evol.db`；保持从项目根目录启动，可以持续读取同一份任务记录。

### 通过 API 使用

登录后携带 Bearer Token 创建异步审查任务：

```powershell
$session = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8080/v1/auth/login `
  -ContentType 'application/json' `
  -Body (@{username='admin'; password='<你的管理员密码>'} | ConvertTo-Json)
$headers = @{Authorization="Bearer $($session.access_token)"}

$task = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8080/v1/reviews?async=true' `
  -Headers $headers -ContentType 'application/json' `
  -Body (@{
    repository = 'demo/api'
    mode = 'agentic'
    diff = "--- a/app.py`n+++ b/app.py`n@@ -1 +1 @@`n-result = parse(user_input)`n+result = eval(user_input)`n"
  } | ConvertTo-Json)

Invoke-RestMethod -Headers $headers "http://127.0.0.1:8080/v1/tasks/$($task.task_id)"
# 任务完成后获取 Markdown 报告
Invoke-WebRequest -Headers $headers "http://127.0.0.1:8080/v1/tasks/$($task.task_id)/report"
```

## GitHub 接入

接入后，PR 的 `opened`、`reopened` 和 `synchronize` 事件会触发异步审查。

```text
GitHub PR 事件 → 公网 HTTPS /webhooks/github → 签名校验 → 下载 Diff → 审查任务
```

1. 为服务配置随机的 `EVOL_GITHUB_WEBHOOK_SECRET`。私有仓库访问、评论回写或修复发布还需要配置 `EVOL_GITHUB_TOKEN`。
2. 在仓库 **Settings → Webhooks → Add webhook** 中填写 Payload URL：`https://<你的服务域名>/webhooks/github`，Content type 选择 `application/json`，Secret 与服务配置一致，事件选择 **Pull requests**。
3. 创建或更新一个 PR，在 GitHub 的 Recent Deliveries 中查看请求，在 evol 的任务中心查看审查结果。正常接收的请求返回 `202`。

本地运行时需要将公网 HTTPS 地址转发到本地服务；GitHub 无法直接访问 `127.0.0.1`。Webhook 使用 HMAC-SHA256 签名认证，无需管理台 Bearer Token，其 Secret 应与 `EVOL_AUTH_SECRET` 分开设置。公开服务前需启用登录；反向代理可仅公开 Webhook 路径。

默认不回写评论。设置 `EVOL_AUTO_POST_REVIEW=true` 后，审查完成时会创建或更新 PR 评论。GitHub Token 的仓库权限应覆盖实际使用的功能：读取 PR、写评论，以及按需创建修复分支、提交和 Draft PR。

修复通过任务的 `/fix` 接口单独触发，需要启用目标仓库的自动修复权限，并设置 `EVOL_REPAIR_TEST_COMMAND`。模型补丁需通过校验，且完整仓库副本上的修改前、修改后测试均通过，才会发布到新的 `evol/fix-pr-*` 分支并创建 Draft PR；未配置测试命令时只返回建议补丁。Python 文件会进行编译和词法检查，其他语言的验证依赖配置的测试命令。测试在临时仓库副本中通过子进程执行。

## Skills 与反馈演进

`skills/` 保存按领域组织的审查协议，包括安全、正确性、可靠性、性能、数据库、API 兼容性、测试质量、代码质量和可观测性。每个包以 `SKILL.md` 为入口，包含 `name`、`description` 等元数据，以及具体的调查步骤、证据标准和输出要求。

Lead 先读取 Skill 目录信息，再按任务选择相关 Skill；选中的指令注入对应 Worker 的上下文。`allowed-tools` 声明与角色权限取交集，附带文本资源按需读取。Skill 提供审查方法，实际工具由运行时注册。

反馈演进包含两个独立的版本对象：

| 对象 | 用途 | API 入口 |
|---|---|---|
| 审查提示词 | 根据失败案例调整审查指令，回放比较新旧版本 | `/v1/evolution/*` |
| Agent Skill 包 | 对 `SKILL.md` 及附带资源生成候选、评测、激活和回滚 | `/v1/skill-evolution/*` |

候选版本通过配置的改进与回归门禁后才能激活。反馈确认、样本质量和模型表现共同影响结果，版本机制本身不代表审查效果必然提升。

## 常用接口

除登录、健康检查和使用签名认证的 Webhook 外，启用认证后访问业务接口需要 Bearer Token；管理操作还受角色权限约束。

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/health` | 查看服务和模型配置状态 |
| `POST` | `/v1/auth/login` | 登录并获取 Token |
| `POST` | `/v1/reviews` | 同步审查；加 `?async=true` 使用异步队列 |
| `GET` | `/api/tasks` | 查询任务列表 |
| `GET` | `/v1/tasks/{id}` | 查询任务状态、轨迹与报告 |
| `GET` | `/v1/tasks/{id}/report` | 获取 Markdown 报告 |
| `POST` | `/v1/tasks/{id}/cancel`、`/v1/tasks/{id}/resume` | 取消或续跑任务 |
| `GET/POST` | `/v1/tasks/{id}/feedback` | 查询或提交反馈 |
| `POST` | `/v1/tasks/{id}/fix` | 生成并验证修复补丁 |
| `POST` | `/webhooks/github` | 接收 GitHub PR 事件 |
| `GET` | `/api/skills` | 查询已加载的 Skill |
| `POST` | `/v1/skills/reload` | 重新加载磁盘 Skill |
| `GET` | `/v1/evolution/status`、`/v1/skill-evolution/status` | 查询演进门禁与版本状态 |
| `POST` | `/v1/evolution/auto`、`/v1/skill-evolution/auto` | 生成候选并执行回放评测 |
| `GET` | `/metrics` | 获取 Prometheus 指标 |

全部 HTTP 路由定义见 [`evol/api.py`](evol/api.py)。

## 运行配置

配置统一使用 `EVOL_` 前缀，更多参数见 [`.env.example`](.env.example)。

| 配置 | 默认值或用途 |
|---|---|
| `EVOL_HOST` / `EVOL_PORT` | `127.0.0.1` / `8080` |
| `EVOL_DB_PATH` | SQLite 文件，默认 `evol.db` |
| `EVOL_DATABASE_URL` | 配置后使用 PostgreSQL |
| `EVOL_REDIS_URL` | 配置后使用 Redis Streams 队列，支持 ACK、租约、重试与死信处理 |
| `EVOL_MAX_DIFF_BYTES` | 默认 `1048576`，即 1 MiB |
| `EVOL_MAX_STEPS` / `EVOL_TIMEOUT_SECONDS` | 审查运行时默认最多 8 步、120 秒；运行时步数不等于模型调用次数 |
| `EVOL_AGENT_TOKEN_BUDGET` / `EVOL_AGENT_TIME_BUDGET_SECONDS` | 角色执行预算 |
| `EVOL_SKILLS_DIR` | Skill 包目录，默认 `skills` |
| `EVOL_REPAIR_TEST_COMMAND` | 修复前后验证使用的目标仓库测试命令 |
| `EVOL_OTEL_ENDPOINT` | 可选 OpenTelemetry 导出端点 |

仓库还提供 [`docker-compose.yml`](docker-compose.yml)，用于组合启动应用、PostgreSQL 和 Redis。容器部署前同样需要配置模型、认证密钥和管理员密码，并使用持久化卷保存数据。
