"""分析模型客户端与仓库上下文构建。

分析只做一次结构化模型调用，上下文来自小规模 grep 结果；
MVP 刻意不引入代码索引或多 Agent 分析。"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from app.core.config import AnalysisPolicy


class ClarificationQuestion(BaseModel):
    """澄清问题：优先单选，确需自由输入时才使用 text。"""

    text: str
    type: Literal["single_choice", "text"] = "single_choice"
    options: list[str] = Field(default_factory=list)
    allow_other: bool = True


class AnalysisResult(BaseModel):
    """分析阶段的结构化输出。"""
    need_info: bool = False
    questions: list[ClarificationQuestion | str] = Field(default_factory=list)
    root_cause: str = ""
    confidence: float = 0.0
    evidence: list[str] = Field(default_factory=list)
    candidate_files: list[str] = Field(default_factory=list)
    plan: list[str] = Field(default_factory=list)
    test_plan: str = ""
    test_command: str = ""
    risk: str = "LOW"
    rollback: str = ""
    assumptions: list[str] = Field(default_factory=list)


class Analyzer(Protocol):
    async def analyze(self, *, issue_key: str, title: str, body: str, repo_path: str) -> AnalysisResult:
        ...


@dataclass
class RepoContext:
    """发送给模型的小型仓库上下文（文件列表 + grep 片段）。"""
    files: list[str]
    snippets: list[tuple[str, str]]

    def as_prompt(self) -> str:
        parts = ["Repository files (first 3000):", "\n".join(self.files)]
        parts.append("\nRelevant snippets:")
        for path, text in self.snippets:
            parts.append(f"\n--- {path} ---\n{text}")
        return "\n".join(parts)


class ContextBuilder:
    """基于 git ls-files 和 ripgrep 构建有界上下文。"""
    def __init__(self, max_files: int = 3000, max_snippets: int = 10, snippet_lines: int = 200):
        self.max_files = max_files
        self.max_snippets = max_snippets
        self.snippet_lines = snippet_lines

    def build(self, repo_path: str, terms: list[str]) -> RepoContext:
        root = Path(repo_path)
        try:
            files = subprocess.run(
                ["git", "ls-files"],
                cwd=root,
                text=True,
                capture_output=True,
                check=True,
                timeout=30,
            ).stdout.splitlines()[: self.max_files]
        except (subprocess.SubprocessError, OSError):
            files = []

        snippets: list[tuple[str, str]] = []
        if terms:
            pattern = "|".join(re.escape(t) for t in terms if t.strip())
            if pattern:
                try:
                    hits = subprocess.run(
                        ["rg", "-l", "-i", pattern, "."],
                        cwd=root,
                        text=True,
                        capture_output=True,
                        timeout=30,
                    ).stdout.splitlines()
                except (subprocess.SubprocessError, OSError):
                    hits = []
                for hit in hits[: self.max_snippets]:
                    rel = hit.removeprefix("./")
                    try:
                        text = (root / rel).read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    snippets.append((rel, "\n".join(text.splitlines()[: self.snippet_lines])))
        return RepoContext(files=files, snippets=snippets)


def extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found")
    return json.loads(text[start : end + 1])


class AnthropicAnalyzer:
    """基于 Anthropic 兼容 Messages API 的分析器。"""
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        max_tokens: int = 32000,
        context: ContextBuilder | None = None,
        policy: AnalysisPolicy | None = None,
    ):
        from anthropic import AsyncAnthropic

        self.policy = policy or AnalysisPolicy()
        self.client = AsyncAnthropic(
            api_key=api_key,
            base_url=base_url,
            timeout=self.policy.timeout_seconds,
        )
        self.model = model
        self.max_tokens = max_tokens
        self.context = context or ContextBuilder()

    async def analyze(self, *, issue_key: str, title: str, body: str, repo_path: str) -> AnalysisResult:
        terms = [issue_key, *re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", title)[:8]]
        ctx = self.context.build(repo_path, terms)
        conventions = (
            "\n".join(f"- {item}" for item in self.policy.conventions)
            or "- Follow the existing repository patterns and test framework."
        )
        assumption_rule = (
            "If a missing detail has an obvious safe default, proceed with that default "
            "and record it in `assumptions`; do not ask a question."
            if self.policy.allow_default_assumptions
            else "Ask before assuming any missing product behavior."
        )
        question_example = (
            '{"text": "...", "type": "single_choice", '
            '"options": ["A", "B"], "allow_other": true}'
        )
        prompt = f"""You are a senior engineer. Analyze the bug and return ONLY JSON.

Bug: {issue_key}
Title: {title}
Description:
{body}

{ctx.as_prompt()}

Return JSON with exactly these fields:
need_info (bool), questions (object[]), root_cause (string), confidence (0..1),
evidence (string[] with file:line), candidate_files (string[]), plan (string[]),
test_plan (string), test_command (string), risk (LOW|MEDIUM|HIGH), rollback (string),
assumptions (string[]).

Rules:
- Do not modify code.
- Inspect the repository before asking anything. Never ask for file paths, CSS selectors,
  test runner names, or code snippets that can be discovered from the repository.
- {assumption_rule}
- Ask at most {self.policy.max_questions} questions, and only when the missing information
  blocks correctness, safety, data migration, credentials, or a material business rule.
- For each question, prefer `type: "single_choice"` with 2-4 concrete options.
  Add `"allow_other": true` so the reporter can enter a custom answer.
  Use `type: "text"` only when the answer cannot be enumerated.
- For common UI/UX behavior, prefer conventional, accessible defaults and document them
  in `assumptions` instead of asking.
- Question object format:
  {question_example}
- Every root cause must cite at least one file:line in evidence.
- test_command must be runnable and target the specific regression test.

Project conventions:
{conventions}
"""
        # 长分析可能超过 10 分钟；Anthropic 要求这类请求使用流式接口。
        # 仍然在流结束后拿到完整 Message，再统一做 JSON 解析。
        async with asyncio.timeout(self.policy.timeout_seconds):
            async with self.client.messages.stream(
                model=self.model,
                max_tokens=self.max_tokens,
                messages=[{"role": "user", "content": prompt}],
                timeout=self.policy.timeout_seconds,
            ) as stream:
                response = await stream.get_final_message()
        text = "".join(
            getattr(block, "text", "")
            for block in response.content
            if getattr(block, "type", "") == "text"
        )
        data = extract_json(text)
        return AnalysisResult.model_validate(data)


class StaticAnalyzer:
    """用于测试和离线演示的确定性分析器。"""

    def __init__(self, result: AnalysisResult):
        self.result = result

    async def analyze(self, *, issue_key: str, title: str, body: str, repo_path: str) -> AnalysisResult:
        return self.result