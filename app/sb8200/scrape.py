"""
Implementation of the scrape and metric update functions
"""

import ssl
from collections import OrderedDict
from dataclasses import dataclass
from datetime import timedelta
from typing import TypeAlias, TypedDict

import structlog
from aiohttp import BasicAuth, ClientSession
from bs4 import BeautifulSoup
from err.exceptions import (
    ModemHtmlError,
    ModemNotOkError,
    ModemUnauthorizedError,
    NoAuthTokenError,
)
from sb8200 import metrics, parse

log = structlog.get_logger(__name__)

CONN_STATUS_ENDPOINT = "/cmconnectionstatus.html"
PROD_INFO_ENDPOINT = "/cmswinfo.html"

# Modem uses very old TLS so we need to bend over backwards to pretend it's 2010
##
# pylint: disable = protected-access / W0212
legacy_ssl_context = ssl._create_unverified_context(
    protocol=ssl.PROTOCOL_SSLv23,
    purpose=ssl.Purpose.SERVER_AUTH,
    check_hostname=False,
)
legacy_ssl_context.set_ciphers("AES128-GCM-SHA256")


class MetricConfig(TypedDict, total=False):
    metric: metrics.Gauge | metrics.Counter | metrics.Enum
    flags: str | None
    previous: dict[str, float]
    values: dict[str, str] | None
    default: str | None


MetricsMap: TypeAlias = OrderedDict[str, MetricConfig | None]


@dataclass
class ChannelSnapshot:
    """Validated channel values ready to publish as one snapshot."""

    channel_ids: set[str]
    numeric_values: list[tuple[MetricConfig, str, float]]
    cumulative_values: dict[str, dict[str, int]]


async def do_login(cs: ClientSession, login: str, password: str) -> str:
    """
    Login and return the CSRF token that must be included in all other requests.
    """
    if login is None or password is None:
        raise NoAuthTokenError("Missing modem username or password")

    # For reasons that I don't understand, the modem wants the encoded username:password
    # in both the Authorization: Basic header AND the URL.
    # If the token is not sent in the URL, the modem will redirect to the login page.
    # If the full basic auth string is not sent in the headers, the modem will redirect to the login page.
    ##
    auth = BasicAuth(login, password)
    _auth_token = auth.encode().split(" ")[1]
    login_url_fragment = f"{CONN_STATUS_ENDPOINT}?login_{_auth_token}"

    # Attempt logging in. If credentials are accepted, we'll get a CSRF token
    ##
    # s_meta_scrape_time has only one label: scrape_target
    with metrics.s_meta_scrape_time.labels("login").time():
        async with cs.request(
            method="GET",
            url=login_url_fragment,
            auth=auth,
            ssl=legacy_ssl_context,
        ) as resp:
            metrics.c_meta_scrape_result.labels(resp.status, "login").inc()
            if resp.status != 200:
                # In testing, only ever 200 and 401. A 401 is a
                #   ModemUnauthorizedError: NOT necessarily bad credentials --
                #   the modem 401s the first login after a reboot with correct
                #   credentials -- so main.py retries it a bounded number of
                #   times. Any other non-200 is a plain ModemNotOkError.
                payload = await resp.text()
                if resp.status == 401:
                    raise ModemUnauthorizedError(payload=payload)
                _e = f"Failed to log in. Status={resp.status}."
                raise ModemNotOkError(_e, status_code=resp.status, payload=payload)

            csrf_token = await resp.text()
            log.debug("CSRF Token", token=csrf_token)
            log.debug("Cookie jar", count=len(cs.cookie_jar))
    log.info("Done with login.")
    return csrf_token


async def do_modem_scrape(
    cs: ClientSession,
    csrf_token: str,
) -> tuple[BeautifulSoup, BeautifulSoup]:
    """
    Requests the firs HTML pages that contain the bulk of useful/graphable data from modem.

    See notes in re_notes/auth.md
    I don't know if it's a bug with the client-side JS, something on the modem or just a timing related thing but
    when using a browser, requesting the modem product information right after landing on the connection info page
    usually results in being sent back to log in.

    Sometimes I can get around this by navigating to some other tab and then back to the product info tab but that's flaky.

    For now, I'm going to go with the "log in, get csrf and then hit both targets" approach.
    Hitting them both in quick succession seems to work which makes me think it's timing related but who knows.
    """

    log.debug("Attempting to get connection status")
    # With token, we can now attempt to get the data we want
    data_url_fragment = f"{CONN_STATUS_ENDPOINT}?ct_{csrf_token}"

    # If this worked, it'll take about 10s for the data to come back!
    ##
    with metrics.s_meta_scrape_time.labels("connection_data").time():
        async with cs.request(
            method="GET", url=data_url_fragment, ssl=legacy_ssl_context
        ) as resp:
            metrics.c_meta_scrape_result.labels(resp.status, "connection_data").inc()
            if resp.status != 200:
                _e = f"Failed to get connection status. Status={resp.status}"
                payload = await resp.text()
                raise ModemNotOkError(_e, status_code=resp.status, payload=payload)
            raw_connection_state = await resp.text()

    # Quickly try to get the product info
    log.info("Attempting to get product info...")
    # With token, we can now attempt to get the data we want
    product_info_url_fragment = f"{PROD_INFO_ENDPOINT}?ct_{csrf_token}"

    # This comes back a lot quicker than the connection status
    ##
    with metrics.s_meta_scrape_time.labels("product_info").time():
        async with cs.request(
            method="GET", url=product_info_url_fragment, ssl=legacy_ssl_context
        ) as resp:
            metrics.c_meta_scrape_result.labels(resp.status, "product_info").inc()
            if resp.status != 200:
                _e = f"Failed to get product info. Status={resp.status}"
                payload = await resp.text()
                raise ModemNotOkError(_e, status_code=resp.status, payload=payload)
            raw_product_info = await resp.text()

    # Assuming nothing went wrong, we can now parse the HTML
    return BeautifulSoup(raw_connection_state, "html.parser"), BeautifulSoup(
        raw_product_info, "html.parser"
    )


def update_connection_channel_metrics(bs: BeautifulSoup) -> bool:
    """Attempts to update connection status related metrics from parsed HTML.

    Args:
        bs (BeautifulSoup): parsed HTML from channel status page
    """

    # It feels odd trying to shove the current datetime into a metric.
    # I'm not really sure what the diagnostic value of that is.
    # Testing that a datetime could be parsed out at all is valuable, though.
    # It's worth the effort for the uptime though as that has a lot of diagnostic value.
    ##
    datetime_ok = True
    try:
        parsed_time = parse.get_current_system_time(bs)
    except ModemHtmlError as exc:
        datetime_ok = False
        log.warning(
            "Could not parse modem system time; HTML may have changed", error=exc
        )
    else:
        if parsed_time is None:
            # An unset clock is a valid, expected part of a disconnected page.
            log.debug("Modem clock unset; no DOCSIS link")
        else:
            log.debug("Modem has datetime of", dt=parsed_time)
    _record_parse_result("datetime", datetime_ok)

    snapshots: dict[str, tuple[ChannelSnapshot, MetricsMap]] = {}
    sections = (
        (
            "conn_downstream",
            parse.extract_downstream_channels(bs),
            metrics.DS_METRICS,
        ),
        ("conn_upstream", parse.extract_upstream_channels(bs), metrics.US_METRICS),
    )

    for parse_target, channel_data, metrics_map in sections:
        if channel_data is None:
            log.error("Could not find channel table", parse_target=parse_target)
            continue

        try:
            snapshot = _parse_channel_snapshot(channel_data, metrics_map)
        except ModemHtmlError as exc:
            log.error(
                "Could not validate channel table",
                parse_target=parse_target,
                error=exc,
            )
            continue

        snapshots[parse_target] = (snapshot, metrics_map)

    expected_targets = {"conn_downstream", "conn_upstream"}
    channels_ok = snapshots.keys() == expected_targets
    if channels_ok:
        # Both tables validated before either direction is mutated, preventing
        # a malformed half-scrape from publishing a mixed old/new snapshot.
        for parse_target in ("conn_downstream", "conn_upstream"):
            snapshot, metrics_map = snapshots[parse_target]
            log.info(
                "Updating channel metrics",
                parse_target=parse_target,
                count=len(snapshot.channel_ids),
            )
            _apply_channel_snapshot(snapshot, metrics_map)

    # A direction is only successful when the complete two-table snapshot was
    # both validated and published. Record exactly one result per target/poll.
    for parse_target in expected_targets:
        _record_parse_result(parse_target, channels_ok)

    return datetime_ok and channels_ok


def update_modem_metrics(prod_info_html: BeautifulSoup) -> bool:
    """
    Most of what we get back from this request is strings that we can massage into Info() class metric.
    We can do this because the value is not expected to change often so we're not going to blow up
        TSDB cardinality by doing this.
    """
    modem_info = parse.get_modem_info(prod_info_html)

    # Uptime is the ONE value that we don't want to stuff into Info()
    _uptime = modem_info.pop("up_time")
    # Assuming we were able to parse uptime, we need to convert the string to a timedelta which
    #   allows us to get the number of seconds which we can then pass to the gauge
    # E.G.: uptime will be parsed as something like '46 days 12h:55m:21s.00' -> 4.021×10^6 seconds
    ##
    uptime_ok = isinstance(_uptime, timedelta)
    if not uptime_ok:
        # In testing, most of the time, modem returns the full HTML and parsing uptime is easy.
        # But when the uptime is not present, the rest of the modem info also isn't present.
        # So if this fails, we can use it as a reliable proxy for the rest of the modem info.
        log.error(
            "Failed to parse product_info/uptime as timedelta. Scrape issue?",
            uptime=_uptime,
        )
    else:
        metrics.g_modem_uptime_seconds.set(_uptime.total_seconds())

    # As noted above, if we couldn't parse the uptime we probably couldn't parse the rest of the modem info
    # The uptime field is the only one that's not a string and we've already popped it.
    # Any info that we have left will either be None or Some(str).
    ##
    modem_info = {k: v for k, v in modem_info.items() if isinstance(v, str)}
    # do not pass in empty dict; this resets the labels on the metric so we'll lose the last known good value
    if len(modem_info) > 0:
        metrics.i_modem_info.info(modem_info)
    else:
        log.warning(
            "Can't update modem_info metric; no parsed data!", modem_info=modem_info
        )

    product_info_ok = uptime_ok and len(modem_info) == 4
    _record_parse_result("product_info", product_info_ok)
    return product_info_ok


def update_connection_metrics(connection_info_html: BeautifulSoup) -> bool:
    """
    This is not the "static" product info, this is high-level connection info.
    Things like DOCSIS version, provisioning file status ... etc.
    This info comes from the top of the connection info page but I'm treating it as
        distinct from the channel specific information/metrics
    """
    # Similar to how we did the channel metrics, we have an ordered dict that maps the key to the metric
    ##
    # startup_info will be a dict where the keys come from the HTML and should match the keys in SU_METRICS
    startup_info = parse.extract_startup_procedure(connection_info_html)
    expected_procedures = set(metrics.SU_METRICS)
    missing_procedures = expected_procedures - startup_info.keys()
    if missing_procedures:
        log.error(
            "Startup table is missing expected procedures",
            missing=sorted(missing_procedures),
        )
        _record_parse_result("startup", False)
        return False

    pending_gauges: list[tuple[metrics.Gauge, str, float]] = []
    pending_enums: list[tuple[metrics.Enum, str, str]] = []
    try:
        for procedure, metric_data in metrics.SU_METRICS.items():
            if metric_data is None:
                continue

            data = startup_info[procedure]
            metric = metric_data["metric"]
            comment = data["Comment"]
            if isinstance(metric, metrics.Gauge):
                value = float(data["Value"].split(maxsplit=1)[0])
                pending_gauges.append((metric, comment, value))
            elif isinstance(metric, metrics.Enum):
                values_map = metric_data["values"]
                if values_map is None:
                    raise ModemHtmlError(
                        f"Startup Enum {procedure!r} has no value mapping"
                    )
                value = values_map.get(data["Value"], metric_data["default"])
                if value is None:
                    raise ModemHtmlError(
                        f"Startup Enum {procedure!r} has no default state"
                    )
                pending_enums.append((metric, comment, value))
            else:
                raise ModemHtmlError(
                    f"Unsupported startup metric for procedure {procedure!r}"
                )
    except (AttributeError, ModemHtmlError, TypeError, ValueError) as exc:
        log.error("Could not validate startup table", error=exc)
        _record_parse_result("startup", False)
        return False

    # Validation is complete, so replacing volatile comment-labelled children
    # cannot leave a half-parsed snapshot behind.
    for metric_data in metrics.SU_METRICS.values():
        if metric_data is not None:
            metric_data["metric"].clear()

    for metric, comment, value in pending_gauges:
        metric.labels(comment).set(value)
    for metric, comment, value in pending_enums:
        metric.labels(comment).state(value)

    _record_parse_result("startup", True)
    return True


def _record_parse_result(parse_target: str, success: bool) -> None:
    metrics.c_meta_parse_result.labels(parse_target, success).inc()


def _parse_channel_snapshot(
    ch_data: list[list[str]], metrics_map: MetricsMap
) -> ChannelSnapshot:
    """Validate and normalize a channel table without mutating metrics."""
    headers = list(metrics_map)
    if "Channel ID" not in headers:
        raise ModemHtmlError("Channel metric map has no 'Channel ID' column")

    channel_id_index = headers.index("Channel ID")
    channel_ids: set[str] = set()
    numeric_values: list[tuple[MetricConfig, str, float]] = []
    cumulative_values: dict[str, dict[str, int]] = {}

    for row_index, row in enumerate(ch_data):
        if len(row) != len(headers):
            raise ModemHtmlError(
                f"Row {row_index} has {len(row)} columns; expected {len(headers)}"
            )

        try:
            channel_id = str(int(row[channel_id_index]))
        except ValueError as exc:
            raise ModemHtmlError(
                f"Row {row_index} has invalid channel ID {row[channel_id_index]!r}"
            ) from exc

        if channel_id in channel_ids:
            raise ModemHtmlError(f"Duplicate channel ID {channel_id!r}")
        channel_ids.add(channel_id)

        for column_index, metric_config in enumerate(metrics_map.values()):
            if metric_config is None or column_index == channel_id_index:
                continue

            raw_value = row[column_index].strip()
            if metric_config["flags"] is not None:
                if not raw_value:
                    raise ModemHtmlError(
                        f"Row {row_index}, column {headers[column_index]!r} is empty"
                    )
                counts = cumulative_values.setdefault(headers[column_index], {})
                counts[raw_value] = counts.get(raw_value, 0) + 1
                continue

            try:
                value = float(raw_value.split(maxsplit=1)[0])
            except (IndexError, ValueError) as exc:
                raise ModemHtmlError(
                    f"Row {row_index}, column {headers[column_index]!r} "
                    f"has invalid numeric value {raw_value!r}"
                ) from exc
            if isinstance(metric_config["metric"], metrics.Counter) and value < 0:
                raise ModemHtmlError(
                    f"Row {row_index}, column {headers[column_index]!r} "
                    f"has negative counter value {value}"
                )
            numeric_values.append((metric_config, channel_id, value))

    return ChannelSnapshot(channel_ids, numeric_values, cumulative_values)


def _apply_channel_snapshot(snapshot: ChannelSnapshot, metrics_map: MetricsMap) -> None:
    """Replace the exported channel snapshot after validation has succeeded."""
    gauge_metrics = {
        metric_config["metric"]
        for metric_config in metrics_map.values()
        if metric_config is not None
        and isinstance(metric_config["metric"], metrics.Gauge)
    }
    for metric in gauge_metrics:
        metric.clear()

    for metric_config in metrics_map.values():
        if metric_config is None or not isinstance(
            metric_config["metric"], metrics.Counter
        ):
            continue
        previous_values = metric_config.setdefault("previous", {})
        for channel_id in set(previous_values) - snapshot.channel_ids:
            metric_config["metric"].remove(channel_id)
            del previous_values[channel_id]

    for metric_config, channel_id, current_value in snapshot.numeric_values:
        metric = metric_config["metric"]
        if isinstance(metric, metrics.Gauge):
            metric.labels(channel_id=channel_id).set(current_value)
            continue

        if not isinstance(metric, metrics.Counter):
            raise TypeError(f"Unsupported channel metric type: {type(metric)!r}")

        previous_values = metric_config.setdefault("previous", {})
        previous_value = previous_values.get(channel_id)
        # The modem exposes an absolute total that resets when it reboots. Turn
        # that into a process-monotonic Prometheus Counter by adding deltas, or
        # the new raw value when a reset is detected.
        if previous_value is None or current_value < previous_value:
            increment = current_value
        else:
            increment = current_value - previous_value
        metric.labels(channel_id=channel_id).inc(increment)
        previous_values[channel_id] = current_value

    for column_name, counts in snapshot.cumulative_values.items():
        metric_config = metrics_map[column_name]
        if metric_config is None or not isinstance(
            metric_config["metric"], metrics.Gauge
        ):
            raise TypeError(f"Cumulative column {column_name!r} is not a Gauge")
        for value, count in counts.items():
            metric_config["metric"].labels(value).set(count)


def _do_metrics_update(ch_data: list[list[str]], metrics_map: MetricsMap) -> None:
    """Compatibility wrapper for callers that update one validated table."""
    snapshot = _parse_channel_snapshot(ch_data, metrics_map)
    _apply_channel_snapshot(snapshot, metrics_map)
