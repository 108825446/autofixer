"""配置模型与加载。

所有可调参数集中在这里，工作流代码不得硬编码分支、阈值、
审批人或模型名称。"""

from __future__ import annotations

import hashlib
import json
import os
from fnmatch import fnmatch
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class AppSection(BaseModel):
    base_url: str = "http://localhost:8080"
    admin_token_env: str = "AUTOFIX_ADMIN_TOKEN"
    approval_secret_env: str = "AUTOFIX_APPROVAL_SECRET"
    database_url: str = "postgresql+asyncpg://autofix:autofix@localhost:5432/autofix"


class SourceSection(BaseModel):
    type: str = "gitlab_issues"
    project: str = ""
    labels: list[str] = Field(default_factory=list)
    poll_interval_seconds: int = 300
    endpoint: str | None = None
    token_env: str = "SOURCE_TOKEN"


class Branches(BaseModel):
    base: str = "develop"
    test: str = "test"
    allow_squash: bool = False


class Limits(BaseModel):
    max_files: int = 10
    max_lines: int = 200
    allowed_paths: list[str] = Field(default_factory=lambda: ["src/main/**", "src/test/**"])
    forbidden_paths: list[str] = Field(
        default_factory=lambda: [
            "pom.xml",
            "build.gradle",
            "package-lock.json",
            ".gitlab-ci.yml",
            "ci/**",
            "deploy/**",
            "db/migration/**",
        ]
    )


class GateModule(BaseModel):
    """单模块门禁配置；paths 用于按变更文件选择模块。"""

    paths: list[str] = Field(default_factory=lambda: ["**"])
    working_dir: str = "."
    compile: str = "./mvnw -q -DskipTests compile"
    unit: str = "./mvnw -q -Dtest='!*IT' test"
    regression: str = "./mvnw -q -Dtest={test_class} test"
    timeout_seconds: int = 600


class Gates(BaseModel):
    """默认门禁 + 可选的多模块门禁（monorepo 前后端）。"""

    # 命令相对于仓库根目录的工作目录；monorepo 可设为 backend/frontend
    working_dir: str = "."
    compile: str = "./mvnw -q -DskipTests compile"
    unit: str = "./mvnw -q -Dtest='!*IT' test"
    regression: str = "./mvnw -q -Dtest={test_class} test"
    timeout_seconds: int = 600
    # 有 modules 时按变更文件选择；没有 modules 时使用上面的默认配置
    modules: dict[str, GateModule] = Field(default_factory=dict)

    def resolve(self, files: list[str]) -> list[tuple[str, GateModule]]:
        """按变更文件选择需要执行的模块门禁。"""

        if not self.modules:
            return [("default", GateModule(
                paths=["**"], working_dir=self.working_dir,
                compile=self.compile, unit=self.unit,
                regression=self.regression, timeout_seconds=self.timeout_seconds,
            ))]
        matched = [
            (name, module)
            for name, module in self.modules.items()
            if any(fnmatch(path, pattern) for path in files for pattern in module.paths)
        ]
        if matched:
            return matched
        return [("default", GateModule(
            paths=["**"], working_dir=self.working_dir,
            compile=self.compile, unit=self.unit,
            regression=self.regression, timeout_seconds=self.timeout_seconds,
        ))]


class Deploy(BaseModel):
    mode: Literal["MERGE_ONLY", "LOCAL_DEPLOY"] = "MERGE_ONLY"
    command: str | None = None
    health_url: str | None = None
    rollback_command: str | None = None


class Delivery(BaseModel):
    merge_mode: Literal["AUTO_MERGE_IF_ALLOWED", "CREATE_MR_ONLY"] = "AUTO_MERGE_IF_ALLOWED"
    deploy: Deploy = Field(default_factory=Deploy)


class ApprovalGate(BaseModel):
    approver: Literal["assignee", "code_owner"] = "assignee"
    fallback: list[str] = Field(default_factory=list)
    ttl_hours: int = 24
    max_reject: int = 2


class ApprovalGates(BaseModel):
    plan: ApprovalGate = Field(default_factory=lambda: ApprovalGate(ttl_hours=24, max_reject=2))
    result: ApprovalGate = Field(default_factory=lambda: ApprovalGate(ttl_hours=48))
    merge: ApprovalGate = Field(
        default_factory=lambda: ApprovalGate(approver="code_owner", ttl_hours=8)
    )


class Approval(BaseModel):
    channel: Literal["link", "lark"] = "link"
    user_map: dict[str, str] = Field(default_factory=dict)
    code_owners: list[str] = Field(default_factory=list)
    gates: ApprovalGates = Field(default_factory=ApprovalGates)


class Network(BaseModel):
    mode: str = "proxy"
    proxy_env: str = "HTTPS_PROXY"


class Agent(BaseModel):
    type: str = "opencode"
    protocol: Literal["ACP"] = "ACP"
    command: list[str] = Field(default_factory=lambda: ["opencode", "acp", "--pure"])
    timeout_minutes: int = 40
    max_attempts: int = 3
    network: Network = Field(default_factory=Network)


class Model(BaseModel):
    provider: str = "anthropic"
    model: str = "claude-opus-5-5"
    api_key_env: str = "ANTHROPIC_API_KEY"
    base_url: str | None = None
    max_tokens: int = 32000


class Budget(BaseModel):
    max_usd_per_task: float = 8.0
    max_usd_per_day: float = 200.0


class AnalysisPolicy(BaseModel):
    """分析阶段的默认假设策略，用来减少不必要的澄清往返。"""

    allow_default_assumptions: bool = True
    max_questions: int = 3
    # 单次分析最长等待时间；防止模型/网关无响应时任务永久占用
    timeout_seconds: int = 900
    conventions: list[str] = Field(default_factory=list)


class Project(BaseModel):
    key: str = "order-service"
    repo: str = "group/order-service"
    gitlab_url: str = "https://gitlab.example.com"
    repo_path: str = "repos/order-service"
    branches: Branches = Field(default_factory=Branches)
    limits: Limits = Field(default_factory=Limits)
    gates: Gates = Field(default_factory=Gates)
    delivery: Delivery = Field(default_factory=Delivery)
    approval: Approval = Field(default_factory=Approval)
    agent: Agent = Field(default_factory=Agent)
    model: Model = Field(default_factory=Model)
    analysis: AnalysisPolicy = Field(default_factory=AnalysisPolicy)
    budget: Budget = Field(default_factory=Budget)


class AppConfig(BaseModel):
    """应用、来源系统与项目的根配置。"""
    app: AppSection = Field(default_factory=AppSection)
    source: SourceSection = Field(default_factory=SourceSection)
    project: Project = Field(default_factory=Project)

    @classmethod
    def from_yaml(cls, path: str | Path) -> AppConfig:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.model_validate(data)

    def config_hash(self) -> str:
        """返回审批所依赖配置的稳定哈希。

        允许路径、阈值、门禁命令、分支或审批角色变化时，
        旧审批必须失效。
        """
        payload = {
            "repo": self.project.repo,
            "branches": self.project.branches.model_dump(mode="json"),
            "limits": self.project.limits.model_dump(mode="json"),
            "gates": self.project.gates.model_dump(mode="json"),
            "merge_mode": self.project.delivery.merge_mode,
            "approval_gates": self.project.approval.gates.model_dump(mode="json"),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def admin_token(self) -> str:
        return os.environ.get(self.app.admin_token_env, "")

    def approval_secret(self) -> str:
        return os.environ.get(self.app.approval_secret_env, "")


def load_config(path: str | Path) -> AppConfig:
    """加载 YAML 配置，并应用部署期环境变量覆盖。"""

    config = AppConfig.from_yaml(path)
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        config.app.database_url = database_url
    return config