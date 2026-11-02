"""Tests written before member numbers were required register players without them."""

import pytest

from chips import engine


@pytest.fixture(autouse=True)
def member_numbers_optional(monkeypatch):
    monkeypatch.setattr(engine, "REQUIRE_MEMBER_DEFAULT", False)
    monkeypatch.setattr(engine, "WAITLIST_OFFER_HOURS", 0)  # and expect the next team to move in at once
