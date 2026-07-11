"""Behavior when the modem has no DOCSIS sync (ISP outage / cable unplugged).

The modem's web UI stays reachable during an outage and still serves the
connection-status page, but with an unset clock ("--- --- -- --:--:-- ----") and
a single unsynced downstream channel (id 0). These tests pin that the scraper:

  - does not crash on the placeholder clock (it used to raise ValueError and
    abort the whole scrape),
  - reports the unsynced / access-denied state,
  - drops stale per-channel and startup series left over from a prior healthy
    scrape instead of freezing them at their last-good value,
  - removes every stale per-channel series, including codeword counters.

The clear-before-repopulate behavior makes the gauge/enum series deterministic
per scrape, so these tests do not depend on execution order.
"""

import os

import pytest
from bs4 import BeautifulSoup
from err.exceptions import ModemHtmlError
from prometheus_client import REGISTRY
from sb8200 import parse, scrape

_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _load(name):
    with open(os.path.join(_FIXTURES, name), encoding="utf-8") as fh:
        return BeautifulSoup(fh.read(), "html.parser")


def _scrape(soup):
    """Run the per-poll metric updates the way main.main() does."""
    assert scrape.update_connection_metrics(soup)
    assert scrape.update_connection_channel_metrics(soup)


def _value(name, **labels):
    return REGISTRY.get_sample_value(name, labels)


def test_outage_scrape_reports_unsynced_and_does_not_crash():
    soup = _load("cmconnectionstatus_outage.html")

    # Unset modem clock parses to None rather than raising.
    assert parse.get_current_system_time(soup) is None

    _scrape(soup)  # must not raise

    # The single unsynced channel the modem reports during an outage.
    assert _value("sb8200_downstream_frequency_hz", channel_id="0") == 0.0
    assert _value("sb8200_downstream_power_dbmv", channel_id="0") == -55.6

    # Connectivity and DOCSIS access reflect the outage.
    assert (
        _value(
            "sb8200_startup_connectivity_state",
            comment="Not Synchronized",
            sb8200_startup_connectivity_state="not_ok",
        )
        == 1.0
    )
    assert (
        _value(
            "sb8200_startup_docsis_net_access_state",
            comment="",
            sb8200_startup_docsis_net_access_state="not_allowed",
        )
        == 1.0
    )
    assert (
        _value(
            "sb8200_downstream_channel_lock_status_count",
            lock_status="Not Locked",
        )
        == 1.0
    )
    assert (
        _value("sb8200_downstream_channel_lock_status_count", lock_status="Not") is None
    )


def test_normal_then_outage_drops_stale_series():
    _scrape(_load("cmconnectionstatus_normal.html"))

    # Healthy scrape: real channels present, no phantom channel 0, connectivity ok.
    assert _value("sb8200_downstream_frequency_hz", channel_id="4") is not None
    assert _value("sb8200_downstream_frequency_hz", channel_id="0") is None
    assert (
        _value(
            "sb8200_startup_connectivity_state",
            comment="Operational",
            sb8200_startup_connectivity_state="ok",
        )
        == 1.0
    )

    _scrape(_load("cmconnectionstatus_outage.html"))

    # Stale real channels are dropped (not frozen); only unsynced channel 0 remains.
    assert _value("sb8200_downstream_frequency_hz", channel_id="4") is None
    assert _value("sb8200_downstream_frequency_hz", channel_id="0") == 0.0

    # Stale enum series is dropped: the pre-outage "Operational -> ok" is gone,
    #   only the current "Not Synchronized -> not_ok" remains.
    assert (
        _value(
            "sb8200_startup_connectivity_state",
            comment="Operational",
            sb8200_startup_connectivity_state="ok",
        )
        is None
    )
    assert (
        _value(
            "sb8200_startup_connectivity_state",
            comment="Not Synchronized",
            sb8200_startup_connectivity_state="not_ok",
        )
        == 1.0
    )

    # The old per-channel Counter child is removed along with the Gauges.
    assert _value("sb8200_downstream_corrected_total", channel_id="4") is None

    # Recovery replaces the outage-only channel and startup label in turn.
    _scrape(_load("cmconnectionstatus_normal.html"))
    assert _value("sb8200_downstream_frequency_hz", channel_id="4") is not None
    assert _value("sb8200_downstream_frequency_hz", channel_id="0") is None
    assert (
        _value(
            "sb8200_startup_connectivity_state",
            comment="Operational",
            sb8200_startup_connectivity_state="ok",
        )
        == 1.0
    )
    assert (
        _value(
            "sb8200_startup_connectivity_state",
            comment="Not Synchronized",
            sb8200_startup_connectivity_state="not_ok",
        )
        is None
    )


def test_codeword_counter_only_increments_by_modem_delta():
    normal = _load("cmconnectionstatus_normal.html")
    _scrape(normal)

    channel_id = "193"
    initial = _value("sb8200_downstream_corrected_total", channel_id=channel_id)
    assert initial == 2259597714.0

    # Re-observing the same cumulative modem value must not change the Counter.
    _scrape(normal)
    assert _value("sb8200_downstream_corrected_total", channel_id=channel_id) == initial

    corrected_cell = next(
        row.find_all("td")[6]
        for row in normal.find_all("tr")
        if len(row.find_all("td")) == 8
        and row.find_all("td")[0].get_text(strip=True) == channel_id
    )
    corrected_cell.string = str(int(initial) + 7)
    _scrape(normal)
    assert (
        _value("sb8200_downstream_corrected_total", channel_id=channel_id)
        == initial + 7
    )

    # A lower raw value indicates a modem-side reset; start accumulating from it.
    corrected_cell.string = "3"
    _scrape(normal)
    assert (
        _value("sb8200_downstream_corrected_total", channel_id=channel_id)
        == initial + 10
    )


def test_malformed_channel_snapshot_does_not_replace_last_good_values():
    normal = _load("cmconnectionstatus_normal.html")
    _scrape(normal)
    previous_frequency = _value("sb8200_downstream_frequency_hz", channel_id="4")

    malformed_rows = parse.extract_downstream_channels(normal)
    assert malformed_rows is not None
    malformed_rows[0][3] = "not-a-frequency"

    with pytest.raises(ModemHtmlError):
        scrape._do_metrics_update(malformed_rows, scrape.metrics.DS_METRICS)

    assert (
        _value("sb8200_downstream_frequency_hz", channel_id="4") == previous_frequency
    )


def test_missing_startup_table_records_failure_without_partial_replacement():
    _scrape(_load("cmconnectionstatus_normal.html"))
    labels = {
        "comment": "Operational",
        "sb8200_startup_connectivity_state": "ok",
    }
    assert _value("sb8200_startup_connectivity_state", **labels) == 1.0

    parse_failure_labels = {"parse_target": "startup", "parse_result": "False"}
    failures_before = _value("meta_parse_result_total", **parse_failure_labels) or 0

    missing_table = BeautifulSoup("<html><title>Status</title></html>", "html.parser")
    assert not scrape.update_connection_metrics(missing_table)
    assert _value("sb8200_startup_connectivity_state", **labels) == 1.0
    assert (
        _value("meta_parse_result_total", **parse_failure_labels) == failures_before + 1
    )
