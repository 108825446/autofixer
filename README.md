# Bug 自动修复 MVP

v0.3 设计方案的最小可运行实现：

```text
接入 → 分析 → 方案审批 → ACP 修复 → L1–L3 + Red-Green
→ 结果审批 + 合并授权 → 恒定 cherry-pick 到 test
```

本 MVP **不接 CI，也不回写来源系统**。`DONE` 表示「已合入测试分支」，不表示「已部署」。

## 技术栈

- Python 3.12、FastAPI、Pydantic v2
- PostgreSQL 16、SQLAlchemy 2 async、asyncpg、Alembic
- OpenCode ACP（`opencode acp --pure`）
- GitLab REST API（MR / Issue 操作）
- 签名审批链接 + Jinja2 页面
- Docker Compose：API + Worker + Poller + PostgreSQL + 备份

## 快速启动

```bash
cp .env.example .env
cp config.example.yaml config.yaml
# 修改 .env 和 config.yaml
# 把目标仓库 clone 到 config 中 project.repo_path 指定的位置
git clone <repo-url> repos/order-service

# 首次启动前执行数据库迁移（不要依赖应用启动时 create_all）
docker compose run --rm api alembic upgrade head
docker compose up -d --build
```

`docker-compose.yml` 会注入 `DATABASE_URL`，覆盖 YAML 中的数据库地址，因此同一份 `config.yaml` 可以在本地和容器中使用。

API：<http://localhost:8080> · 健康检查：`/healthz` · 指标：`/metrics`

不使用 Docker：

```bash
uv sync --all-groups
cp .env.example .env
cp config.example.yaml config.yaml
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
uv run python -m app.worker_main
uv run python -m app.poller
```

## 测试

```bash
uv run pytest -q
```

当前测试覆盖：

- 状态机合法/非法迁移、取消、终态
- 审批 token、角色校验、`subject_hash` 失效、RESULT + MERGE 双门禁
- 接入幂等、轮询游标
- Diff 约束：10 文件 / 200 行 / 允许路径 / 禁止路径 / 删除测试
- Red-Green：base FAIL、fix PASS
- merge commit + cherry-pick + patch-id 隔离
- ACP 初始化/会话/提示/权限允许/拒绝（fake ACP server）
- Worker 端到端：分析 → 方案审批 → 编码 → 验证 → 结果+合并审批 → 交付
- Kill Switch、配置哈希
- API 鉴权、审批页、审批决策
- GitLab MR 创建/合并、Issue 拉取（respx mock）

当前结果：**25 passed**；`ruff`、`mypy` 均通过。

## OpenCode / ACP

Runner 必须能直接在 `PATH` 中找到 `opencode`，并且项目或全局配置必须包含：

```json
{
  "$schema": "https://opencode.ai/config.json",
  "permission": {
    "edit": "ask",
    "bash": "ask"
  }
}
```

`runner/opencode.json` 会挂载到 Worker 容器。**如果没有配置 `ask`，OpenCode 会直接写文件，不会发出 `session/request_permission`**；ACP 权限不是安全边界，Worker 仍会检查最终 Diff。

本地验证 ACP：

```bash
python3 scripts/verify_acp.py
```

## GitLab 预检

只读检查合并方式、squash 策略、分支保护和 Token 权限：

```bash
GITLAB_URL=https://gitlab.example.com \
GITLAB_TOKEN=xxxx \
GITLAB_PROJECT=group/order-service \
python3 scripts/verify_gitlab_preflight.py
```

要求：

- `merge_method=merge`，不能是只允许 squash；
- `squash_option != always`；
- `develop`、`test` 已配置分支保护；
- Bot 具备 Developer/Maintainer 权限；
- 如果 `only_allow_merge_if_pipeline_succeeds=true` 且未接 CI，自动合并会被阻塞；应设置 `delivery.merge_mode=CREATE_MR_ONLY`，或调整分支保护。

## 交付语义

- 一次修复只产生一个 `fix_sha`；Agent 产生多个 commit 时，平台会压成一个。
- MR #1：`bugfix/{key} → develop`，使用 merge commit，禁止 squash。
- 测试分支提升：从 `test` 创建 `promote/{key}`，只 cherry-pick `fix_sha`，并比较 `patch-id`。
- MR #2：`promote/{key} → test`。
- `DONE` = 已合入 test。若 `deploy.mode=LOCAL_DEPLOY`，还必须通过部署命令和健康检查。
- 当前 MVP 不自动回写来源系统；Issue 状态由人工维护。

## 配置

参考 `config.example.yaml`。关键配置：

- `app.database_url`：PostgreSQL 异步 URL。
- `project.repo_path`：Worker 使用的本地仓库路径。
- `project.limits`：10 文件 / 200 行、允许/禁止路径。
- `project.delivery.merge_mode`：`AUTO_MERGE_IF_ALLOWED` 或 `CREATE_MR_ONLY`。
- `project.delivery.deploy.mode`：`MERGE_ONLY` 或 `LOCAL_DEPLOY`。
- `project.approval.code_owners`：合并门禁审批人。
- `project.agent.command`：ACP 命令，默认 `opencode acp --pure`。

## 目录结构

```text
app/
  main.py                 FastAPI 应用组装入口
  worker_main.py          Worker 进程入口
  poller.py               轮询进程入口
  api/
    deps.py               FastAPI 依赖（DB Session、Admin Token）
    routes.py             HTTP API、审批页、管理接口
    serializers.py        API 响应序列化
    templates/            审批页模板
  core/
    config.py             配置模型、加载、config_hash
    db.py                 Engine/Session 工厂、建表
    models.py             7 张表
    security.py           哈希、审批令牌、subject_hash
    state_machine.py      合法状态迁移与审计事件
    metrics.py            Prometheus 指标
  integrations/
    gitlab.py             GitLab REST 客户端
    gitops.py             Git Worktree / cherry-pick / patch-id
    notify.py             通知适配器
  agents/
    acp.py                ACP JSON-RPC 客户端 + 权限策略
    analyzer.py           模型分析 + 仓库上下文构建
  workflow/
    approvals.py          PLAN / RESULT / MERGE 审批
    intake.py             事件模型 + GitLab Issues 适配器
    verifier.py           L1-L3、Red-Green、Diff 约束
    delivery.py           MR1 → cherry-pick → MR2 → 可选部署
    worker.py             状态处理器与任务编排
    poller.py             来源系统轮询循环
tests/                    确定性测试，不依赖网络
scripts/                  ACP 与 GitLab 预检验证脚本
migrations/               Alembic 初始迁移
```

## MVP 已知限制

- 不触发/不解析 CI，仅支持可选的本地部署。
- 单服务、单 Worker 进程，任务串行处理；后续可拆 Worker 或迁 Temporal。
- 隔离是「专用 Runner + Worktree + 权限约束」，不是每任务容器/gVisor。
- 不支持 Merge Queue、CODEOWNERS 自动解析、多 Git Provider、多租户。
- 审批身份目前来自表单值 + token 映射；生产应接入飞书/SSO 身份。
- 当前来源适配器只实现了 GitLab Issues；接禅道/内部平台需要补 `BugSourceAdapter`。

完整设计与升级触发条件见：

`docs/design/bug-auto-fix-mvp-v0.3-implementation.md`