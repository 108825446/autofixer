# MVP 前置验证报告（2026-11-15）

> 验证对象：v0.3 实施基线中的两个前置问题
> 1. OpenCode ACP 是否可用（初始化、会话、提示、权限允许/拒绝）
> 2. GitLab MR + merge commit + cherry-pick 是否可行
>
> 环境：macOS Darwin 25.4.0；OpenCode 1.19.5；Git 2.39.5；当前环境**没有 GitLab URL/Token**

---

## 1. 结论

| # | 验证项 | 结果 | 说明 |
|---|--------|------|------|
| 1 | OpenCode ACP 协议 | **PASS** | `opencode acp` 存在；initialize/session/new/prompt/update 正常 |
| 2 | ACP 权限允许 | **PASS** | 配置 `permission.edit=ask` 后触发 `session/request_permission`；allow_once 成功写入 |
| 3 | ACP 权限拒绝 | **PASS** | reject_once 后文件未创建 |
| 4 | Git 本地语义（merge commit + cherry-pick + patch-id） | **PASS** | 修复提交在 merge 后仍可达；cherry-pick 后 patch-id 一致；test 只多修复文件 |
| 5 | 直接 merge bugfix → test 的隔离性 | **PASS（验证了问题）** | 直接 merge 会带入 develop 的无关提交；证明必须 cherry-pick |
| 6 | GitLab MR API / 合并 API / 分支保护 | **NOT RUN** | 当前环境无 GitLab URL/Token；需用预检脚本在真实项目验证 |

**总体判断：ACP 主链路可用，但权限不能只依赖 ACP 回调；Git 交付语义已验证，GitLab API 仍需一次真实项目预检。**

---

## 2. ACP 验证

### 2.1 环境

```text
opencode 1.19.5
opencode acp --help  → 存在 ACP server 子命令
```

### 2.2 验证结果

| 步骤 | 请求/事件 | 结果 |
|------|-----------|------|
| initialize | `initialize {protocolVersion:1, clientCapabilities:{}}` | 返回 `protocolVersion=1`；`loadSession=true`；`sessionCapabilities: resume/fork/list/close` |
| session/new | `session/new {cwd, mcpServers:[]}` | 返回 `sessionId` 与 `configOptions`（model/mode） |
| prompt（读） | 读取 `hello.txt` | 返回 `hello-acp`；`stopReason=end_turn` |
| permission（允许） | 写 `allowed.txt`，配置 `permission.edit=ask` | 收到 `session/request_permission`，选项 `allow_once/allow_always/reject_once`；选 `allow_once` 后写入成功 |
| permission（拒绝） | 写 `denied.txt`，选 `reject_once` | 工具状态 `failed`；文件未创建 |
| usage | `usage_update` | 返回 token/cost 信息 |

### 2.3 关键发现

1. **默认配置不会触发权限请求。**  
   在 `opencode.json` 只有 `$schema` 时，Agent 直接写入文件，没有 `session/request_permission`。必须在项目或全局配置中显式设置：
   ```json
   {
     "$schema": "https://opencode.ai/config.json",
     "permission": {
       "edit": "ask",
       "bash": "ask"
     }
   }
   ```
2. **ACP 权限回调不是安全边界。**  
   即使配置了 `ask`，客户端仍必须：
   - 在 Worker 侧做 Diff/路径/命令白名单校验；
   - 在 Runner 侧限制文件系统与网络；
   - 不能把“ACP 已允许”当作“操作安全”。
3. **权限请求响应格式**：`session/request_permission` 的 `id` 可能为 `0`；响应为：
   ```json
   {"jsonrpc":"2.0","id":0,"result":{"outcome":{"outcome":"selected","optionId":"once"}}}
   ```
   拒绝同理，选 `reject` 选项。
4. **模型未显式锁定。**  
   `session/new` 返回的 `configOptions` 含 model/mode，默认模型是 OpenCode 自带配置。生产必须在 `opencode.json` 或 ACP 配置里显式指定模型，并记录实际模型。
5. **事件类型**：至少处理 `available_commands_update`、`tool_call`、`tool_call_update`、`agent_message_chunk`、`agent_thought_chunk`、`usage_update`。

### 2.4 未验证项

- `session/cancel` 的实际中断行为；
- `session/resume` / `loadSession` 的跨进程恢复；
- 不同 OpenCode 版本的协议兼容性；
- CodeBuddy Code / Pi 的 ACP 行为。

这些不影响 MVP 主链路，但应在 Week 2 前补测 cancel/resume。

---

## 3. Git 交付语义验证

### 3.1 场景

```text
develop: D0 → D1(feature1) → D2(feature2)
test:    D0 → T1(hotfix)
bugfix:  D2 → F(fix + regression test)
```

### 3.2 结果

| 检查 | 结果 |
|------|------|
| `bugfix` 合入 `develop`（merge commit）后 `fix_sha` 是否可达 | **是** |
| 从 `test` 创建 `promote`，`cherry-pick fix_sha` | **成功** |
| `fix_sha` 与 `promote` 的 `patch-id` | **一致** |
| `test..promote` 的变更文件 | 只有 `app.txt`、`regression.txt`（本次修复） |
| 直接 `merge bugfix → test` 的变更文件 | `app.txt`、`feature1.txt`、`feature2.txt`、`regression.txt` |
| 直接 merge 带入的提交 | `F` + `D1` + `D2` |

### 3.3 结论

- 本地 Git 语义完全支持 v0.3 的「merge commit + cherry-pick + patch-id」。
- **直接 merge bugfix → test 会把 develop 上的无关提交带入 test**，实测确认，不能作为默认路径。
- `fix_sha` 在 merge commit 后仍可达，因此 cherry-pick 可行。
- 需要保证修复分支只有一个提交；多提交在推送前压成一个。

---

## 4. GitLab API 验证（未执行）

### 4.1 为什么没执行

当前环境没有：

```text
GITLAB_URL
GITLAB_TOKEN
GITLAB_PROJECT
```

也没有 `glab` CLI。无法在真实 GitLab 上创建 MR、合并、验证分支保护。

### 4.2 已提供的预检脚本

```bash
GITLAB_URL=https://gitlab.example.com \
GITLAB_TOKEN=xxxx \
GITLAB_PROJECT=group/order-service \
python3 scripts/verify_gitlab_preflight.py
```

只读检查：

- token 身份；
- `merge_method` 是否为 `merge`；
- `squash_option` 是否为 `always`；
- 是否要求流水线成功（无 CI 时会阻断自动合并）；
- `develop` / `test` 是否受保护；
- 当前用户是否有 Developer/Maintainer 权限；
- 分支是否存在。

### 4.3 还需要一次真实写入验证

在 sandbox 项目执行：

1. 创建 `bugfix/test-<timestamp>` 分支并提交一个空改动；
2. 创建 MR → `develop`，`squash=false`；
3. 合并，确认返回 `merge_commit_sha`，且 `fix_sha` 仍可达；
4. 从 `test` 创建 `promote/test-<timestamp>`，cherry-pick `fix_sha`；
5. 比较 `patch-id`；
6. 创建 MR → `test`，合并；
7. 验证 `test` 只多出修复提交。

> 这一步会写真实仓库，必须在 sandbox 项目、明确授权后执行。

---

## 5. 对 v0.3 设计的修改

| # | 修改 | 原因 |
|---|------|------|
| 1 | Runner 的 `opencode.json` 必须包含 `permission.edit=ask`、`permission.bash=ask` | 默认配置不会触发权限请求 |
| 2 | 增加 Worker 侧 Diff/路径/命令校验，不能只依赖 ACP 权限 | ACP 权限不是安全边界 |
| 3 | ACP 客户端显式设置模型，记录实际模型 | `session/new` 默认模型不可控 |
| 4 | `scripts/verify_acp.py` 纳入 Week 1 冒烟 | 可重复验证 ACP |
| 5 | `scripts/verify_gitlab_preflight.py` 纳入 Week 1 前置检查 | 提前发现 merge_method/squash/pipeline 阻断 |
| 6 | GitLab 合并前检查 `merge_method=merge`、`squash_option!=always` | 否则无法保留单提交 |
| 7 | 无 CI 且 `only_allow_merge_if_pipeline_succeeds=true` 时，自动降级 `CREATE_MR_ONLY` | 不绕过分支保护 |

---

## 6. 下一步

1. 用真实 GitLab sandbox 项目跑 `verify_gitlab_preflight.py`，确认 `merge_method=merge`、分支保护、权限。
2. 在 sandbox 项目做一次 MR → merge → cherry-pick → MR 的写入验证。
3. 补测 ACP `session/cancel` 与 `session/resume`。
4. 把 `opencode.json` 权限配置写入 Runner 镜像/项目模板。
5. 开始 Week 1 的 PostgreSQL Schema + `bug_task` 队列 + 接入。