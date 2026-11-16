"""API 暴露的 Prometheus 指标定义。"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, generate_latest

TASKS_STARTED = Counter("autofix_tasks_started_total", "Tasks created from intake")
TASKS_COMPLETED = Counter("autofix_tasks_completed_total", "Tasks completed")
TASKS_FAILED = Counter("autofix_tasks_failed_total", "Tasks failed or manual")
APPROVALS = Counter("autofix_approval_decisions_total", "Approval decisions", ["gate", "decision"])
TASKS_BY_STATE = Gauge("autofix_tasks_by_state", "Tasks by state", ["state"])


def render() -> bytes:
    return generate_latest()