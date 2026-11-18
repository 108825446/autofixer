from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.agents.analyzer import AnthropicAnalyzer


class FakeContext:
    def build(self, repo_path: str, terms: list[str]):
        return SimpleNamespace(as_prompt=lambda: "repo context")


class FakeStream:
    def __init__(self, text: str):
        self.text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get_final_message(self):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.text)])


class FakeMessages:
    def __init__(self, text: str):
        self.text = text
        self.kwargs = None

    def stream(self, **kwargs):
        self.kwargs = kwargs
        return FakeStream(self.text)


class FakeClient:
    def __init__(self, text: str):
        self.messages = FakeMessages(text)


@pytest.mark.asyncio
async def test_analysis_prompt_formats_single_choice_example():
    payload = {
        "need_info": False,
        "questions": [
            {
                "text": "滚动方向？",
                "type": "single_choice",
                "options": ["从右向左", "从左向右"],
                "allow_other": True,
            }
        ],
        "root_cause": "CSS 缺少 nowrap",
        "confidence": 0.8,
        "evidence": ["frontend/src/styles.css:1"],
        "candidate_files": ["frontend/src/styles.css"],
        "plan": ["增加 nowrap"],
        "test_plan": "新增滚动测试",
        "test_command": "npm test",
        "risk": "LOW",
        "rollback": "回滚本次提交",
        "assumptions": [],
    }
    analyzer = AnthropicAnalyzer(
        api_key="test",
        model="test-model",
        context=FakeContext(),
    )
    fake = FakeClient(json.dumps(payload, ensure_ascii=False))
    analyzer.client = fake

    result = await analyzer.analyze(
        issue_key="BUG-1",
        title="标题换行",
        body="标题过长时换行",
        repo_path=".",
    )
    assert result.root_cause == "CSS 缺少 nowrap"
    assert "single_choice" in fake.messages.kwargs["messages"][0]["content"]


class SlowStream(FakeStream):
    async def get_final_message(self):
        import asyncio

        await asyncio.sleep(3)
        return await super().get_final_message()


class SlowClient(FakeClient):
    def __init__(self):
        self.messages = type("M", (), {"stream": lambda self, **kwargs: SlowStream("{}")})()


@pytest.mark.asyncio
async def test_analysis_times_out():
    from app.core.config import AnalysisPolicy

    analyzer = AnthropicAnalyzer(
        api_key="test",
        model="test-model",
        context=FakeContext(),
        policy=AnalysisPolicy(timeout_seconds=1),
    )
    analyzer.client = SlowClient()
    with pytest.raises(TimeoutError):
        await analyzer.analyze(
            issue_key="BUG-1",
            title="超时",
            body="body",
            repo_path=".",
        )
