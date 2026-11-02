"""Tests written before member numbers were required register players without them."""

import pytest

from chips import engine


@pytest.fixture(autouse=True)
def member_numbers_optional(monkeypatch):
    monkeypatch.setattr(engine, "REQUIRE_MEMBER_DEFAULT", False)
