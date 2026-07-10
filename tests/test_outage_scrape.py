"""Behavior when the modem has no DOCSIS sync (ISP outage / cable unplugged).

The modem's web UI stays reachable during an outage and still serves the
connection-status page, but with an unset clock ("--- --- -- --:--:-- ----") and
a single unsynced downstream channel (id 0). These tests pin that the scraper:

  - does not crash on the placeholder clock (it used to raise ValueError and
    abort the whole scrape),
  - reports the unsynced / access-denied state,
  - drops stale per-channel and startup series left over from a prior healthy
    scrape instead of freezing them at their last-good value,
  - leaves the accumulating Summaries (corrected/uncorrectable) intact.

The clear-before-repopulate behavior makes the gauge/enum series deterministic
per scrape, so these tests do not depend on execution order.
"""

import os

from bs4 import BeautifulSoup
from prometheus_client import REGISTRY
from sb8200 import parse, scrape

_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _load(name):
    with open(os.path.join(_FIXTURES, name), encoding="utf-8") as fh:
        return BeautifulSoup(fh.read(), "html.parser")


def _scrape(soup):
    """Run the per-poll metric updates the way main.main() does."""
    scrape.update_connection_metrics(soup)
    scrape.update_connection_channel_metrics(soup)


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
            comment="None",
            sb8200_startup_docsis_net_access_state="not_allowed",
        )
        == 1.0
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

    # Summaries accumulate and must NOT be cleared: the corrected-count for a
    #   real channel survives the outage scrape.
    assert _value("sb8200_downstream_corrected_count", channel_id="4") is not None
