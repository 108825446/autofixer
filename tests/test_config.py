from __future__ import annotations

from app.core.config import load_config


def test_load_config_env_override(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///override.db")
    config = load_config("config.example.yaml")
    assert config.app.database_url == "sqlite+aiosqlite:///override.db"
    assert config.project.repo
    assert config.project.branches.test == "test"

def test_gate_modules_resolve_by_changed_files():
    from app.core.config import GateModule, Gates

    gates = Gates(
        modules={
            "backend": GateModule(paths=["backend/**"], working_dir="backend"),
            "frontend": GateModule(paths=["frontend/**"], working_dir="frontend"),
        }
    )
    assert [name for name, _ in gates.resolve(["frontend/src/app.js"])] == ["frontend"]
    assert [name for name, _ in gates.resolve(["frontend/src/app.js", "backend/src/index.ts"])] == [
        "backend",
        "frontend",
    ]
