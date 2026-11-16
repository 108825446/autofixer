# Bug 自动修复交付流程手册

> 适用版本：MVP v0.3
> API 基地址：`http://localhost:8000`（以 `config.yaml` 的 `app.base_url` 为准）
> 代码目录：`/Users/apple/Documents/project-codebuddy/autofixer`
> 当前目标仓库：`didaspace-group/drawhub`

---

## 0. 一页速查

| 阶段 | 操作 | 接口 / 地址 | 期望状态 |
|---|---|---|---|
| 1. 接入 | 轮询或推送 Bug | `POST /api/intake` | `NEW` |
| 2. 分析 | Worker 自动执行 | 查看 `/api/tasks/{id}/timeline` | `ANALYZING` → `WAITING_PLAN_APPROVAL` 或 `WAITING_INFO` |
| 3. 方案审批 | Bug Owner 打开审批链接 | `GET/POST /a/{token}` | `FIXING` |
| 4. 编码 | ACP Agent 修改代码 | 查看 timeline / Diff Artifact | `VERIFYING` |
| 5. 验证 | L1–L3 + Red-Green | 查看 EVIDENCE Artifact | `WAITING_APPROVAL` |
| 6. 结果审批 | Bug Owner 确认修复 | `GET/POST /a/{token}` | 等待 MERGE |
| 7. 合并审批 | Code Owner 授权合并 | `GET/POST /a/{token}` | `DELIVERING` |
| 8. 交付 | MR #1 → promote → MR #2 | timeline 的 `delivery` | `DONE` |

---

## 1. 前置条件

### 1.1 配置

`config.yaml` 需要至少确认：

```yaml
app:
  base_url: http://localhost:8000

project:
  repo: didaspace-group/drawhub
  gitlab_url: https://gitlab.com
  repo_path: repos/drawhub

  branches:
    base: dev
    test: test
    allow_squash: false

  limits:
    max_files: 10
    max_lines: 200
    allowed_paths:
      - "backend/src/**"
      - "backend/tests/**"
      - "frontend/src/**"
      - "frontend/tests/**"

  gates:
    modules:
      backend:
        paths: ["backend/**"]
        working_dir: backend
        compile: "node --check src/index.ts"
        unit: "npm test"
        regression: "npm test"
      frontend:
        paths: ["frontend/**"]
        working_dir: frontend
        compile: "node --check src/app.js"
        unit: "npm test"
        regression: "npm test"

  approval:
    code_owners: ["didaspace"]

  analysis:
    allow_default_assumptions: true
    max_questions: 3
```

### 1.2 GitLab

- Bot Token：`api`、`write_repository`，Developer 以上
- `dev`、`test` 已存在
- `dev`、`test` 已保护
- 保护分支允许 Bot 合并：
  - `Allowed to merge`: `Developers + Maintainers`
  - `Allowed to push`: `No one`
- `merge_method = merge`
- `squash_option != always`
- 如果设置了“流水线必须成功”，MVP 不接 CI 时要使用：

```yaml
delivery:
  merge_mode: CREATE_MR_ONLY
```

### 1.3 本地仓库

```bash
git clone <repo-url> repos/drawhub
git -C repos/drawhub fetch origin
git -C repos/drawhub rev-parse origin/dev
```

Worker 使用 `origin/dev` 作为修复基线，使用 `origin/test` 创建提升分支。

### 1.4 环境变量

```bash
cp .env.example .env
```

至少设置：

```bash
AUTOFIX_ADMIN_TOKEN=...
AUTOFIX_APPROVAL_SECRET=...
GITLAB_TOKEN=...
SOURCE_TOKEN=...          # 可留空，Poller 会回退 GITLAB_TOKEN
ANTHROPIC_API_KEY=...
ANTHROPIC_BASE_URL=...
FEISHU_WEBHOOK=...        # 可选
```

### 1.5 启动前预检

```bash
cd /Users/apple/Documents/project-codebuddy/autofixer

uv run alembic current
uv run alembic check

GITLAB_URL=https://gitlab.com \
GITLAB_TOKEN="$GITLAB_TOKEN" \
GITLAB_PROJECT=didaspace-group/drawhub \
GITLAB_BASE_BRANCH=dev \
GITLAB_TEST_BRANCH=test \
uv run python scripts/verify_gitlab_preflight.py

uv run python scripts/verify_acp.py
```

要求：

- `alembic current` 为 `d5f386f0f475 (head)`
- GitLab 预检没有 `FAIL`
- ACP 初始化、会话、权限允许/拒绝均 PASS

---

## 2. 角色与审批

| 角色 | 当前值 | 权限 |
|---|---|---|
| Bug Owner | Issue assignee，例如 `didaspace` | PLAN、RESULT |
| Code Owner | `approval.code_owners`，当前 `didaspace` | MERGE |
| 管理员 | `AUTOFIX_ADMIN_TOKEN` | 重试、取消、暂停、补充信息、重新分析 |

两个关键原则：

1. **PLAN 未批准，不建分支、不改代码。**
2. **RESULT 和 MERGE 都批准，才进入交付。**

---

## 3. 完整流程

### 3.1 创建/接入 Bug

推荐先创建真实 GitLab Issue：

- 标签：`bug`、`auto-fix`
- assignee：`didaspace`
- 描述：复现步骤、预期、实际、堆栈、对应测试

如果项目还没有标签，先创建：

```bash
source .env

curl --request POST \
  --header "PRIVATE-TOKEN: $GITLAB_TOKEN" \
  "https://gitlab.com/api/v4/projects/88986701/labels" \
  --data "name=bug&color=#d9534f"

curl --request POST \
  --header "PRIVATE-TOKEN: $GITLAB_TOKEN" \
  "https://gitlab.com/api/v4/projects/88986701/labels" \
  --data "name=auto-fix&color=#1f883d"
```

#### 接入方式 A：Poller

Worker 侧 Poller 按 `source.labels` 每 300 秒拉取。

#### 接入方式 B：Intake API（适合测试）

```bash
source .env

curl -sS -X POST "http://localhost:8000/api/intake" \
  -H "Authorization: Bearer ${AUTOFIX_ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d @- <<'JSON'
{
  "event_id": "manual-<唯一值>",
  "source": "gitlab_issues",
  "issue_key": "<真实 GitLab Issue IID>",
  "title": "Bug 标题",
  "body": "复现步骤...\n预期...\n实际...\n堆栈...",
  "repo": "didaspace-group/drawhub",
  "assignee": "didaspace"
}
JSON
```

注意：

- `issue_key` 必须是真实 IID，便于后续追踪。
- `event_id` 必须唯一，重复事件会幂等。
- JSON 必须使用英文双引号，不能使用中文弯引号 `“”`。

### 3.2 分析

Worker 自动执行：

```text
NEW → ANALYZING
```

可能结果：

| 结果 | 状态 | 说明 |
|---|---|---|
| 信息足够 | `WAITING_PLAN_APPROVAL` | 生成方案，等待审批 |
| 信息不足 | `WAITING_INFO` | 需要补充信息 |
| 分析失败 | `MANUAL` | 查看 `last_error` / timeline |

查看：

```bash
TASK_ID="<task_id>"

curl -sS "http://localhost:8000/api/tasks/${TASK_ID}/timeline?include_content=true" \
  | python3 -m json.tool

curl -sS "http://localhost:8000/api/tasks/${TASK_ID}/artifacts/ANALYSIS" \
  | python3 -m json.tool
```

### 3.3 信息不足时补充

`WAITING_INFO` 时，分析问题在 `ANALYSIS` Artifact 的 `questions` 字段。

```bash
curl -sS -X POST "http://localhost:8000/api/tasks/${TASK_ID}/info" \
  -H "Authorization: Bearer ${AUTOFIX_ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d @- <<'JSON'
{
  "actor": "didaspace",
  "answer": "补充的信息；如果属于通用问题，可写：按项目通用方案处理。"
}
JSON
```

状态：

```text
WAITING_INFO → ANALYZING
```

### 3.4 方案审批

从以下位置获取链接：

1. Worker 日志
2. 飞书 Webhook 通知
3. 本地从数据库生成（见 §5）

审批链接：

```text
http://localhost:8000/a/<token>
```

页面操作：

- `Your identity`：`didaspace`
- `Approve`：批准
- `Request changes`：回到分析
- `Reject`：取消流程

curl 方式：

```bash
curl -sS -X POST "http://localhost:8000/a/<token>" \
  -d "decision=APPROVED" \
  -d "actor=didaspace" \
  -d "comment=同意方案"
```

批准后：

```text
WAITING_PLAN_APPROVAL → FIXING
```

### 3.5 编码

Worker 会：

1. 从 `origin/dev` 的冻结 `base_sha` 创建 `bugfix/{issue_key}`；
2. 在绝对路径 worktree 中启动 OpenCode ACP；
3. 强制修改范围、10 文件 / 200 行；
4. 把多个 Agent commit 压成一个 `fix_sha`；
5. 推送 `bugfix/*`。

状态：

```text
FIXING → VERIFYING
```

查看 Diff：

```bash
curl -sS "http://localhost:8000/api/tasks/${TASK_ID}/artifacts/DIFF" \
  | python3 -m json.tool
```

### 3.6 自动验证

按变更文件选择模块门禁：

- 只改 `frontend/` → 只跑 frontend
- 只改 `backend/` → 只跑 backend
- 两端都改 → 两组都跑

验证内容：

```text
L1 compile
L2 unit
L3 Red-Green
Diff scope
```

通过后：

```text
VERIFYING → WAITING_APPROVAL
```

查看证据：

```bash
curl -sS "http://localhost:8000/api/tasks/${TASK_ID}/artifacts/EVIDENCE" \
  | python3 -m json.tool
```

### 3.7 结果 + 合并审批

`WAITING_APPROVAL` 有两个待审批记录：

| 门禁 | 审批人 | 作用 |
|---|---|---|
| RESULT | Bug Owner | 确认修复结果 |
| MERGE | Code Owner | 授权合入 dev/test |

两个链接都要批准。只批准一个不会进入交付。

批准后：

```text
WAITING_APPROVAL → DELIVERING
```

### 3.8 交付

交付步骤：

```text
MR #1: bugfix/{key} → dev
  → 合并，记录 fix_merge_sha
  → 从 test 创建 promote/{key}
  → 只 cherry-pick fix_sha
  → 校验 patch-id
  → MR #2: promote/{key} → test
  → 合并，记录 test_merge_sha
```

查看：

```bash
curl -sS "http://localhost:8000/api/tasks/${TASK_ID}/timeline?include_content=true" \
  | python3 -m json.tool
```

成功：

```text
DELIVERING → DONE
```

`MERGE_ONLY` 模式下 `DONE` 表示「已合入 test」，不是「已部署」。

### 3.9 流程结束

当前 MVP 不执行来源系统回写。

`DONE` 只表示代码已合入 `test`；来源系统的状态/评论由人工维护，避免把占位 Issue 或错误 IID 当成自动闭环。

---

## 4. 接口清单

| 方法 | 路径 | 鉴权 | 用途 |
|---|---|---|---|
| POST | `/api/intake` | Admin Bearer | 推送 Bug |
| GET | `/api/tasks` | 无 | 任务列表，可加 `?state=WAITING_APPROVAL` |
| GET | `/api/tasks/{id}` | 无 | 任务状态与审批 |
| GET | `/api/tasks/{id}/timeline?include_content=true` | 无 | 完整时间线 |
| GET | `/api/tasks/{id}/artifacts/{type}` | 无 | 最新 Artifact，如 `ANALYSIS`、`DIFF`、`EVIDENCE` |
| POST | `/api/tasks/{id}/info` | Admin Bearer | 补充 WAITING_INFO 信息 |
| POST | `/api/tasks/{id}/reanalyze` | Admin Bearer | 配置变化后重新分析 |
| POST | `/api/tasks/{id}/retry` | Admin Bearer | MANUAL 任务重试 |
| POST | `/api/tasks/{id}/cancel` | Admin Bearer | 取消任务 |
| POST | `/api/admin/pause` | Admin Bearer | 全局 Kill Switch |
| GET | `/healthz` | 无 | 健康检查 |
| GET | `/metrics` | 无 | Prometheus 指标 |
| GET | `/a/{token}` | 一次性 Token | 渲染审批页 |
| POST | `/a/{token}` | 一次性 Token | 提交审批决策 |

带查询参数的 URL 在 zsh 中必须加引号：

```bash
curl "http://localhost:8000/api/tasks?state=WAITING_APPROVAL"
```

---

## 5. 审批链接

### 5.1 链接格式

```text
{app.base_url}/a/{token}
```

Token 由以下内容生成：

```text
approval_id + expires_at + HMAC-SHA256(AUTOFIX_APPROVAL_SECRET)
```

数据库只保存 Token 哈希，不保存明文。

### 5.2 从数据库生成待审批链接

```bash
cd /Users/apple/Documents/project-codebuddy/autofixer
source .env

uv run python - <<'PY'
import asyncio
from dotenv import load_dotenv
from sqlalchemy import select

from app.core.config import load_config
from app.core.db import make_engine, make_session_factory
from app.core.models import Approval
from app.core.security import make_approval_token

TASK = "<task_id>"

async def main():
    load_dotenv(".env")
    config = load_config("config.yaml")
    engine = make_engine(config.app.database_url)
    factory = make_session_factory(engine)

    async with factory() as session:
        rows = (await session.execute(
            select(Approval)
            .where(Approval.task_id == TASK, Approval.decision.is_(None))
            .order_by(Approval.id)
        )).scalars().all()

        for approval in rows:
            token = make_approval_token(
                config.approval_secret(),
                approval.id,
                approval.expires_at,
            )
            print(
                approval.gate,
                f"{config.app.base_url.rstrip('/')}/a/{token}",
                "expires:",
                approval.expires_at,
            )

    await engine.dispose()

asyncio.run(main())
PY
```

**关键：必须 `load_dotenv(".env")`**。否则签名 secret 为空，生成的链接会返回：

```text
approval link is invalid or expired
```

### 5.3 批准命令

```bash
curl -sS -X POST "http://localhost:8000/a/<token>" \
  -d "decision=APPROVED" \
  -d "actor=didaspace" \
  -d "comment=同意"
```

决策值：

```text
APPROVED
CHANGES_REQUESTED
REJECTED
```

---

## 6. 状态查看

### 6.1 快速状态

```bash
TASK_ID="<task_id>"
curl -sS "http://localhost:8000/api/tasks/${TASK_ID}" | python3 -m json.tool
```

### 6.2 完整时间线

```bash
curl -sS "http://localhost:8000/api/tasks/${TASK_ID}/timeline?include_content=true" \
  | python3 -m json.tool
```

包含：

```text
task
events
agent_runs
artifacts
approvals
delivery
```

### 6.3 数据库直接查

```sql
select id, state, version, fix_sha, fix_merge_sha, test_merge_sha, last_error
from bug_task
where id = '<task_id>';

select id, from_state, to_state, event, actor, created_at
from event_log
where task_id = '<task_id>'
order by id;

select id, gate, subject_ref, decision, decided_by
from approval
where task_id = '<task_id>'
order by id;
```

### 6.4 日志

- Worker 前台运行时：终端输出
- Docker：`docker compose logs -f worker`
- API：`docker compose logs -f api`
- 审批链接只在生成时发送，数据库不保存明文

---

## 7. 故障处理

### 7.1 常见错误速查

| 现象 | 原因 | 处理 |
|---|---|---|
| `missing env ANTHROPIC_API_KEY` | 本地未加载 `.env` | 入口已支持 `load_dotenv(".env")`；确认从项目根目录启动 |
| `Streaming is required...` | 分析请求超过 10 分钟，用了非流式 | 已改为 `messages.stream`；重启 Worker 后重试 |
| `JSON decode error` | curl 里用了中文弯引号 | 用英文双引号，推荐 heredoc |
| `zsh: no matches found` | URL 中 `?` 被 zsh 当通配符 | URL 加引号 |
| `403 admin token required` | 未带 Bearer Token | `source .env`，加 `Authorization` |
| `approval link is invalid or expired` | Token 生成 secret 不一致/过期 | 用 `load_dotenv` 重新生成；检查 TTL |
| `approval subject changed` | 配置、分析版本、Diff 或 SHA 变了 | 重新分析或重新审批 |
| `WAITING_INFO` 长期不动 | 未提交补充信息 | 调 `/info`，或检查 Poller/API |
| `FIXING` 报 `No such file... work/fix-...` | worktree 相对路径/旧 Worker | 使用最新代码；清理 `repos/drawhub/work`；重启 Worker |
| `relation "bug_task" already exists` | `create_all` 先建表，Alembic 未 stamp | 空表 `alembic stamp head`；有数据不要删表 |
| `404 .../issues/<id>/notes` | issue_key 不是真实 IID | 用真实 GitLab Issue IID |
| MR 无法自动合并 | 分支保护/流水线门禁/Bot 权限 | 检查保护分支；必要时 `CREATE_MR_ONLY` |
| 门禁在错误目录执行 | 单模块 gate 配置 | 用 `gates.modules`，按 `paths` 选模块 |
| Worker 不领任务 | kill switch / lease / 不可领取状态 | 查 `lease_until`、`next_run_at`、`kill_switch` |
| `DONE` 但 `deployed=false` | `MERGE_ONLY` 模式 | 这是预期；不是故障 |
| 重复创建任务 | `event_id` 不唯一 | 每次推送使用唯一 `event_id` |

### 7.2 任务卡在 `MANUAL`

1. 查 `last_error` 和 timeline；
2. 修复环境/配置/仓库问题；
3. 调：

```bash
curl -sS -X POST "http://localhost:8000/api/tasks/<task_id>/retry" \
  -H "Authorization: Bearer ${AUTOFIX_ADMIN_TOKEN}"
```

4. 如果是方案/配置变化，优先用 `/reanalyze`。

### 7.3 任务卡在 `WAITING_PLAN_APPROVAL`

- 检查是否有 `decision=PENDING` 的 PLAN 审批；
- 检查审批链接是否过期；
- 如果改了 `config_hash`，旧审批会失效；
- 调 `/reanalyze` 生成新方案和新审批。

### 7.4 任务卡在 `FIXING`

检查：

```text
last_error
worktree 是否创建在项目根目录 work/fix-...
OpenCode 是否在 PATH
opencode.json 权限是否为 ask
模型是否可用
attempt 是否达到上限
lease_until 是否未来时间
```

如果失败是路径/旧代码导致，修复后需要：

1. 停止旧 Worker；
2. 清理 `repos/drawhub/work/fix-*`；
3. 重置 `attempt=0`、`lease_until=NULL`、`next_run_at=now()`；
4. 重启 Worker。

### 7.5 验证失败

查看 `EVIDENCE`：

```bash
curl -sS "http://localhost:8000/api/tasks/<task_id>/artifacts/EVIDENCE" \
  | python3 -m json.tool
```

常见原因：

- `Red-Green` base 通过：测试没有复现 Bug；
- `Red-Green` fix 失败：修复没有解决问题；
- 门禁在错误模块执行：检查 `gates.modules` 的 `paths`/`working_dir`；
- 超过 10 文件/200 行：拆小修复或重新审批方案；
- 修改了禁止路径/既有测试：检查 `forbidden_paths` 和测试删除策略。

### 7.6 交付失败

查看 `delivery` 表：

```sql
select step, status, ref, detail from delivery
where task_id = '<task_id>'
order by id;
```

常见：

- MR 冲突 → `MANUAL`；
- `patch-id` 不一致 → `MANUAL`；
- 测试分支带入无关提交 → 检查 promote 分支；
- MR 无法合并 → 检查保护分支和 Bot 权限；
- 构建/部署失败 → `MERGE_ONLY` 下不会发生；`LOCAL_DEPLOY` 下检查 deploy 命令/健康 URL。

### 7.7 紧急停止

全局暂停领取新任务与合并：

```bash
curl -sS -X POST "http://localhost:8000/api/admin/pause?enabled=true" \
  -H "Authorization: Bearer ${AUTOFIX_ADMIN_TOKEN}"
```

恢复：

```bash
curl -sS -X POST "http://localhost:8000/api/admin/pause?enabled=false" \
  -H "Authorization: Bearer ${AUTOFIX_ADMIN_TOKEN}"
```

停止 Worker：

```bash
# 前台 Ctrl-C
# 或
pkill -f "app.worker_main"
```

---

## 8. 验收检查清单

- [ ] Bug 有真实 GitLab IID、`bug`/`auto-fix` 标签、assignee
- [ ] PLAN 审批绑定 `analysis_v{n}` + `config_hash`
- [ ] 修复分支从 `dev` 拉出
- [ ] Diff ≤10 文件 / 200 行
- [ ] L1–L3 和 Red-Green 通过
- [ ] RESULT 和 MERGE 两个审批都通过
- [ ] MR #1 合入 `dev`，`fix_merge_sha` 可追溯
- [ ] `promote/{key}` 只 cherry-pick 一个提交，`patch-id` 一致
- [ ] MR #2 合入 `test`，`test_merge_sha` 可追溯
- [ ] `test` 分支只包含本次修复
- [ ] `DONE` 且 `deployed=false`（MERGE_ONLY）或部署健康（LOCAL_DEPLOY）

---

## 9. 本次验证记录

```text
task_id: 11e4d2a2-d163-4a87-90d9-e4ad80d32a05
issue_key: 123（占位，非真实 Issue；当前 MVP 不回写）
fix_sha: 40359c8bb10a8ab39455c40a6b8f17c251103bd7
fix_merge_sha: 3cbe761f2fcc4aea6035e7a8143b58a138574a77
promote_sha: 1225872b41a04dee270d113817da530fd69a03b9
test_merge_sha: 68401f49b25f112117dfc395aae2fff7e0826bfe
diff: 4 files / 189 lines
gates: frontend compile + npm test + Red-Green PASS
state: DONE
deployed: false
```

结论：核心修复、验证、审批、开发分支合并、测试分支隔离均已完成；当前 MVP 不自动回写来源系统。