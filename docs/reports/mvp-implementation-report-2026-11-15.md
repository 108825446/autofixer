# MVP 实现与测试报告（2026-11-15）

> 对应设计：`docs/design/bug-auto-fix-mvp-v0.3-implementation.md`
> 范围：Python/FastAPI + PostgreSQL + OpenCode ACP + GitLab API；**不接 CI**
> 代码位置：仓库根目录 `app/`、`tests/`、`scripts/`、`migrations/`

---

## 1. 实现结果

| 能力 | 状态 | 位置 |
|------|------|------|
| Bug 接入（推送 + 轮询） | 已实现 | `app/main.py`、`app/workflow/intake.py`、`app/workflow/poller.py` |
| 12 状态机 + 审计 | 已实现 | `app/core/state_machine.py`、`app/core/models.py` |
| PostgreSQL 任务队列 + 租约 | 已实现 | `app/workflow/worker.py`（`FOR UPDATE SKIP LOCKED`） |
| 分析 + 结构化输出 | 已实现 | `app/agents/analyzer.py` |
| PLAN 审批 | 已实现 | `app/workflow/approvals.py`、`app/api/templates/approve_plan.html` |
| OpenCode ACP 编码 | 已实现 | `app/agents/acp.py`、`app/workflow/worker.py` |
| L1–L3 + Red-Green + Diff 约束 | 已实现 | `app/workflow/verifier.py` |
| RESULT + MERGE 双审批 | 已实现 | `app/workflow/approvals.py` |
| 恒定 cherry-pick + patch-id | 已实现 | `app/workflow/delivery.py`、`app/integrations/gitops.py` |
| GitLab MR/Issue 客户端 | 已实现 | `app/integrations/gitlab.py`、`app/workflow/intake.py` |
| 可选本地部署/健康检查 | 已实现 | `app/workflow/delivery.py` |
| 回写来源系统 | 已实现 | `app/workflow/intake.py`、`app/workflow/worker.py` |
| Kill Switch | 已实现 | `app/main.py`、`app/workflow/worker.py` |
| Prometheus metrics | 已实现 | `app/core/metrics.py` |
| Alembic 初始迁移 | 已实现 | `migrations/versions/d5f386f0f475_initial.py` |
| Docker Compose | 已实现 | `docker-compose.yml`、`Dockerfile` |

**未实现（按 v0.3 范围）**：CI 触发、CI 结果解析、自动构建、CI 部署、自动回滚到上一版本（`LOCAL_DEPLOY` 可选）、Merge Queue、CODEOWNERS 自动解析、多租户。

---

## 2. 测试结果

### 2.1 命令

```bash
uv run pytest -q
uv run ruff check app tests scripts
uv run mypy app
ALEMBIC_URL=sqlite+aiosqlite:////tmp/alembic_mvp2.db uv run alembic upgrade head
python3 scripts/verify_acp.py
```

### 2.2 结果

| 检查 | 结果 |
|------|------|
| pytest | **25 passed** |
| ruff | **All checks passed** |
| mypy | **Success: no issues found in 20 source files** |
| Alembic upgrade (SQLite) | **通过**，生成 7 张表 + `alembic_version` |
| 真实 OpenCode ACP 验证 | **PASS**（initialize/session/prompt/permission allow/reject） |
| GitLab 真实 API | **未验证**（当前环境无 URL/Token） |
| Docker build | **未验证**（当前环境没有 docker） |

### 2.3 覆盖矩阵

| 测试文件 | 覆盖 |
|----------|------|
| `test_state_machine.py` | 合法/非法迁移、取消、终态 |
| `test_approvals.py` | token、角色、subject 失效、RESULT+MERGE 双门禁 |
| `test_intake.py` | 幂等接入 |
| `test_poller.py` | 轮询接入 |
| `test_verifier.py` | 10/200、路径、禁止路径、删测试、Red-Green |
| `test_gitops.py` | merge commit、cherry-pick、patch-id、直接 merge 带入无关提交 |
| `test_acp_client.py` | ACP 初始化/会话/提示/权限允许/拒绝（fake server） |
| `test_worker.py` | 端到端：分析→方案审批→编码→验证→结果+合并审批→交付→回写 |
| `test_worker_controls.py` | Kill Switch、配置哈希 |
| `test_api.py` | 健康、鉴权、接入、审批页、审批决策 |
| `test_config.py` | YAML 加载、DATABASE_URL 覆盖 |
| `test_gitlab_client.py` | MR 创建/合并、Issue 拉取（respx mock） |

---

## 3. 关键实现说明

### 3.1 状态与队列

- `bug_task` 同时是状态机和队列；`claim` 使用 `FOR UPDATE SKIP LOCKED`（SQLite 测试时跳过锁）。
- 租约 50 分钟；Worker 崩溃后 `lease_until < now()` 自动重新可领取。
- `SysConfig.kill_switch` 为 true 时 `claim` 直接返回，不再领取新任务。

### 3.2 审批

- `PLAN / RESULT / MERGE` 共用 `approval` 表。
- `RESULT + MERGE` 都在 `WAITING_APPROVAL` 状态内完成；两者都批准才进入 `DELIVERING`。
- `subject_hash` 绑定：
  - PLAN：`analysis_hash + plan_version + base_sha + config_hash`
  - RESULT：`fix_sha + diff_hash + plan_version + config_hash`
  - MERGE：`fix_sha + diff_hash + target_branches + config_hash`
- 审批前复算；不匹配返回 409。

### 3.3 ACP

- `AcpClient` 支持 initialize/session/prompt/update/permission/cancel。
- 权限策略：文件必须在 worktree + allowed_paths；命令白名单；默认拒绝。
- **默认 OpenCode 配置不会触发权限请求**；`runner/opencode.json` 显式设置 `edit=ask`、`bash=ask`。
- ACP 权限不是安全边界；Worker 仍做 Diff 检查。

### 3.4 交付

- 平台把多次 Agent commit 压成一个 `fix_sha`。
- MR #1 合并保留 commit；`promote/{key}` 从 `test` 拉出，只 cherry-pick `fix_sha`。
- `patch-id` 前后比较；不一致转 `MANUAL`。
- `DONE` 表示合入 test；`deployed=false` 时回写 `FIXED_WAITING_DEPLOY`。

---

## 4. 已知限制

1. **GitLab 真实 API 未跑**：需要 sandbox 项目的 URL/Token；预检脚本在 `scripts/verify_gitlab_preflight.py`。
2. **Docker 未构建**：当前环境没有 docker；Dockerfile/Compose 已提供，但需在部署机验证。
3. **无 CI**：不触发/不解析 Pipeline；如果分支保护要求流水线成功，自动合并会被拒绝，应使用 `CREATE_MR_ONLY`。
4. **隔离是专用 Runner + Worktree**，不是每任务容器/gVisor；仅适用于可信内部仓库。
5. **单服务单 Worker**：无横向扩展；超过 ~50/天再拆 Worker/Temporal。
6. **审批身份是表单值 + token 映射**；生产应接飞书/SSO 身份。

---

## 5. 启动方式

```bash
cp .env.example .env
cp config.example.yaml config.yaml
docker compose up -d --build
```

或本地：

```bash
uv sync --all-groups
uv run uvicorn app.main:app --reload
uv run python -m app.worker_main
uv run python -m app.poller
```

测试：

```bash
uv run pytest -q
```