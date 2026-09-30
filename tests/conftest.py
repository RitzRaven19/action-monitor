import pytest


@pytest.fixture(autouse=True)
def offline_search(monkeypatch):
    """The suite never hits the network: web_search uses its offline fixtures
    unless a test explicitly opts back into the live backend."""
    monkeypatch.setenv("SEARCH_BACKEND", "fixtures")
