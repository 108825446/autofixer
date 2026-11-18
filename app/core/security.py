"""规范化哈希与审批 Token 基础函数。

审批绑定 `subject_hash`，其输入包含 Artifact/Diff、代码 SHA 和
配置哈希，确保对象变化后不能复用旧审批。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(value: str | bytes) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def hash_token(token: str) -> str:
    return sha256_hex(token)


def make_approval_token(secret: str, approval_id: int, expires_at: datetime) -> str:
    exp = int(expires_at.timestamp())
    msg = f"{approval_id}:{exp}".encode()
    sig = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    raw = f"{approval_id}:{exp}:{sig}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def parse_approval_token(token: str) -> tuple[int, int, str] | None:
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        approval_id, exp, sig = raw.split(":", 2)
        return int(approval_id), int(exp), sig
    except (ValueError, UnicodeDecodeError):
        return None


def verify_approval_token(secret: str, token: str, now: datetime | None = None) -> int | None:
    parsed = parse_approval_token(token)
    if parsed is None:
        return None
    approval_id, exp, sig = parsed
    msg = f"{approval_id}:{exp}".encode()
    expected = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    current = now or datetime.now(UTC)
    if current.timestamp() > exp:
        return None
    return approval_id


def plan_subject_hash(analysis_hash: str, analysis_version: int, base_sha: str, config_hash: str) -> str:
    return sha256_hex(
        canonical_json(
            {
                "analysis_hash": analysis_hash,
                "analysis_version": analysis_version,
                "base_sha": base_sha,
                "config_hash": config_hash,
            }
        )
    )


def result_subject_hash(
    fix_sha: str, diff_hash: str, analysis_version: int, config_hash: str
) -> str:
    return sha256_hex(
        canonical_json(
            {
                "fix_sha": fix_sha,
                "diff_hash": diff_hash,
                "analysis_version": analysis_version,
                "config_hash": config_hash,
            }
        )
    )


def merge_subject_hash(
    fix_sha: str, diff_hash: str, target_branches: list[str], config_hash: str
) -> str:
    return sha256_hex(
        canonical_json(
            {
                "fix_sha": fix_sha,
                "diff_hash": diff_hash,
                "target_branches": sorted(target_branches),
                "config_hash": config_hash,
            }
        )
    )


def is_expired(expires_at: datetime, now: datetime | None = None) -> bool:
    current = now or datetime.now(UTC)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return current > expires_at

def make_clarification_token(
    secret: str,
    task_id: str,
    analysis_version: int,
    expires_at: datetime,
) -> str:
    """生成绑定任务和当前分析版本的澄清链接 Token。"""

    exp = int(expires_at.timestamp())
    msg = f"{task_id}:{analysis_version}:{exp}".encode()
    sig = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    raw = f"{task_id}:{analysis_version}:{exp}:{sig}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def verify_clarification_token(
    secret: str,
    token: str,
    now: datetime | None = None,
) -> tuple[str, int] | None:
    """校验澄清 Token，返回 (task_id, analysis_version)。"""

    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        task_id, version, exp, sig = raw.split(":", 3)
        analysis_version = int(version)
        expires = int(exp)
    except (ValueError, UnicodeDecodeError):
        return None

    msg = f"{task_id}:{analysis_version}:{expires}".encode()
    expected = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None

    current = now or datetime.now(UTC)
    if current.timestamp() > expires:
        return None
    return task_id, analysis_version
