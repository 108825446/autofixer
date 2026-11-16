from __future__ import annotations

import pytest

from app.core.models import BugTask
from app.core.state_machine import TransitionError, transition


def test_happy_path(task: BugTask):
    transition(task, "analyze")
    assert task.state == "ANALYZING"
    transition(task, "analysis_ready")
    assert task.state == "WAITING_PLAN_APPROVAL"
    transition(task, "plan_approved")
    assert task.state == "FIXING"
    transition(task, "fix_committed")
    assert task.state == "VERIFYING"
    transition(task, "verification_passed")
    assert task.state == "WAITING_APPROVAL"
    transition(task, "both_approved")
    assert task.state == "DELIVERING"
    transition(task, "delivery_succeeded")
    assert task.state == "DONE"
    assert task.version == 7


def test_illegal_transition(task: BugTask):
    with pytest.raises(TransitionError):
        transition(task, "delivery_succeeded")


def test_cancel_from_non_terminal(task: BugTask):
    transition(task, "analyze")
    transition(task, "cancel")
    assert task.state == "CANCELLED"


def test_cancel_from_terminal_is_illegal(task: BugTask):
    task.state = "DONE"
    with pytest.raises(TransitionError):
        transition(task, "cancel")