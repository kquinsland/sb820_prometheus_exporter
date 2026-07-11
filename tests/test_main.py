"""Polling-loop behavior for transient modem and parse failures."""

import asyncio

import pytest
from bs4 import BeautifulSoup
from err.exceptions import ModemNotOkError, ModemUnauthorizedError
from prometheus_client import REGISTRY

import app.main as exporter_main


class StopPolling(BaseException):
    """Stop the intentionally infinite polling loop in a test."""


class FakeCookieJar:
    def __init__(self):
        self.clear_count = 0

    def __len__(self):
        return 0

    def clear(self):
        self.clear_count += 1


class FakeClient:
    def __init__(self):
        self.cookie_jar = FakeCookieJar()


def test_persistent_401_keeps_polling_after_fast_retries(monkeypatch):
    client = FakeClient()
    login_attempts = 0
    sleep_delays = []

    async def unauthorized(*_args):
        nonlocal login_attempts
        login_attempts += 1
        raise ModemUnauthorizedError()

    async def stop_after_slow_retry(delay):
        sleep_delays.append(delay)
        if len(sleep_delays) == 3:
            raise StopPolling

    monkeypatch.setattr(exporter_main, "LOGIN_401_MAX_RETRIES", 2)
    monkeypatch.setattr(exporter_main, "do_login", unauthorized)
    monkeypatch.setattr(exporter_main.asyncio, "sleep", stop_after_slow_retry)

    with pytest.raises(StopPolling):
        asyncio.run(exporter_main.poll_modem(client, "admin", "password"))

    assert login_attempts == 3
    assert sleep_delays == [
        exporter_main.RE_LOGIN_INTERVAL_SECONDS,
        exporter_main.RE_LOGIN_INTERVAL_SECONDS,
        exporter_main.METRICS_POLL_INTERVAL_SECONDS,
    ]
    assert client.cookie_jar.clear_count == 3


def test_non_200_response_is_retried_after_poll_interval(monkeypatch):
    client = FakeClient()
    delays = []

    async def not_ok(*_args):
        raise ModemNotOkError("temporary modem error", status_code=503)

    async def stop(delay):
        delays.append(delay)
        raise StopPolling

    monkeypatch.setattr(exporter_main, "do_login", not_ok)
    monkeypatch.setattr(exporter_main.asyncio, "sleep", stop)

    with pytest.raises(StopPolling):
        asyncio.run(exporter_main.poll_modem(client, "admin", "password"))

    assert delays == [exporter_main.METRICS_POLL_INTERVAL_SECONDS]
    assert client.cookie_jar.clear_count == 1


def test_timeout_is_rate_limited_and_marks_scrape_failed(monkeypatch):
    client = FakeClient()
    delays = []

    async def timeout(*_args):
        raise TimeoutError("modem did not respond")

    async def stop(delay):
        delays.append(delay)
        raise StopPolling

    monkeypatch.setattr(exporter_main, "do_login", timeout)
    monkeypatch.setattr(exporter_main.asyncio, "sleep", stop)

    with pytest.raises(StopPolling):
        asyncio.run(exporter_main.poll_modem(client, "admin", "password"))

    assert delays == [exporter_main.METRICS_POLL_INTERVAL_SECONDS]
    assert client.cookie_jar.clear_count == 1
    assert REGISTRY.get_sample_value("meta_scrape_success") == 0


def test_successful_poll_sets_freshness_metrics(monkeypatch):
    client = FakeClient()
    status_page = BeautifulSoup("<html><title>Status</title></html>", "html.parser")

    async def login(*_args):
        return "csrf"

    async def scrape(*_args):
        return status_page, status_page

    async def stop(_delay):
        raise StopPolling

    monkeypatch.setattr(exporter_main, "do_login", login)
    monkeypatch.setattr(exporter_main, "do_modem_scrape", scrape)
    monkeypatch.setattr(exporter_main, "update_connection_metrics", lambda _page: True)
    monkeypatch.setattr(
        exporter_main, "update_connection_channel_metrics", lambda _page: True
    )
    monkeypatch.setattr(exporter_main, "update_modem_metrics", lambda _page: True)
    monkeypatch.setattr(exporter_main.asyncio, "sleep", stop)

    with pytest.raises(StopPolling):
        asyncio.run(exporter_main.poll_modem(client, "admin", "password"))

    assert REGISTRY.get_sample_value("meta_scrape_success") == 1
    assert REGISTRY.get_sample_value("meta_last_success_unixtime") > 0
