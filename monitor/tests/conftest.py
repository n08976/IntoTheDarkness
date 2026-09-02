from __future__ import annotations

import pytest

from intothedarkness.config import Settings
from intothedarkness.storage import Repository
from intothedarkness.storage.db import Database


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        db_url=f"sqlite:///{tmp_path / 'test.db'}",
        targets_file=tmp_path / "targets.yaml",
        rules_file=tmp_path / "rules.yaml",
        per_host_delay=0.0,
        respect_robots=False,
        alert_cooldown_minutes=60,
    )


@pytest.fixture
def db(settings) -> Database:
    settings.ensure_dirs()
    database = Database(settings.resolved_db_url())
    database.create_all()
    return database


@pytest.fixture
def repo(db) -> Repository:
    return Repository(db)
