from __future__ import annotations

import pytest

from intothedarkness.config import Settings
from intothedarkness.storage import Repository
from intothedarkness.storage.db import Database


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Settings isolated from the developer's environment.

    pydantic-settings reads `.env` and the real environment, so without this a
    populated local `.env` silently changes test outcomes — a credential sitting
    on the machine made "this channel is unconfigured" tests pass a real key.
    """
    return Settings(
        _env_file=None,
        # Explicitly blank so an exported ITD_* variable cannot leak in either.
        resend_api_key="",
        smtp_host="",
        smtp_user="",
        smtp_password="",
        email_from="",
        email_to=[],
        webhook_url="",
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


@pytest.fixture(autouse=True)
def _no_real_watchlist(settings, tmp_path):
    # Settings() defaults to the real vendor list and the published URL. A
    # test that touched either would load hundreds of real vendors, fetch from
    # GitHub, and route matches to a channel that is not configured.
    settings.watchlist_file = tmp_path / "vendors.txt"
    settings.watchlist_url = ""
    return settings
