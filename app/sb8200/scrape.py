"""
Implementation of the scrape and metric update functions
"""

import http.cookies
import ssl
from collections import OrderedDict
from datetime import timedelta

import structlog
from aiohttp import ClientSession
from bs4 import BeautifulSoup
from err.exceptions import ModemNotOkError, NoAuthTokenError
from sb8200 import metrics, parse

log = structlog.get_logger(__name__)

CONN_STATUS_ENDPOINT = "/cmconnectionstatus.html"
PROD_INFO_ENDPOINT = "/cmswinfo.html"


async def do_modem_scrape(
    cs: ClientSession,
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

    # Modem uses very old TLS so we need to bend over backwards to pretend it's 2010
    ##
    # pylint: disable = protected-access / W0212
    legacy_ssl_context = ssl._create_unverified_context(
        protocol=ssl.PROTOCOL_SSLv23,
        purpose=ssl.Purpose.SERVER_AUTH,
        check_hostname=False,
    )
    legacy_ssl_context.set_ciphers("AES128-GCM-SHA256")

    # For reasons that I don't understand, the modem wants a PORTION of the Basic Auth string in the URL.
    # If the token is not sent in the URL, the modem will redirect to the login page.
    # If the full basic auth string is not sent in the headers, the modem will redirect to the login page.
    ##
    # Pylance is technically correct here; there is _a chance_ that the auth token is None.
    if cs.auth is not None:
        _auth_token = cs.auth.encode().split(" ")[1]
    else:
        raise NoAuthTokenError("No auth token found")

    login_url_fragment = f"{CONN_STATUS_ENDPOINT}?login_{_auth_token}"

    # Attempt logging in. If credentials are accepted, we'll get a CSRF token
    ##
    # s_meta_scrape_time has only one label: scrape_target
    with metrics.s_meta_scrape_time.labels("login").time():
        async with cs.request(
            method="GET", url=login_url_fragment, ssl=legacy_ssl_context
        ) as resp:
            metrics.c_meta_scrape_result.labels(resp.status, "login").inc()
            if resp.status != 200:
                # In testing, i've only ever seen 401 and 200s
                # ALSO interesting, JUST AFTER REBOOT, 401 with correct credentials ... so don't make this
                #   a total panic failure
                if resp.status == 401:
                    # Exception: Failed to log in. Status=401
                    _e = f"Modem indicated authentication details are incorrect. Check for extra/incorrect quotes in your env-vars? Status={resp.status}."
                else:
                    _e = f"Failed to log in. Status={resp.status}."
                raise ModemNotOkError(_e)

            csrf_token = await resp.text()
            log.debug("CSRF Token", token=csrf_token)

            # We need to parse the cookies and update the client's cookie jar
            set_cookie_header = resp.headers.get("Set-Cookie")
            if set_cookie_header:
                cookie = http.cookies.SimpleCookie()
                cookie.load(set_cookie_header)
                cs.cookie_jar.update_cookies(cookie)

            log.debug("Cookie jar", count=len(cs.cookie_jar))

    log.info("Done with login... attempting to get connection status data!")
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
                raise ModemNotOkError(_e)
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
                raise ModemNotOkError(_e)
            raw_product_info = await resp.text()

    # Assuming nothing went wrong, we can now parse the HTML
    return BeautifulSoup(raw_connection_state, "html.parser"), BeautifulSoup(
        raw_product_info, "html.parser"
    )


def update_connection_channel_metrics(bs: BeautifulSoup) -> None:
    """Attempts to update connection status related metrics from parsed HTML.

    Args:
        bs (BeautifulSoup): parsed HTML from channel status page
    """

    # It feels odd trying to shove the current datetime into a metric.
    # I'm not really sure what the diagnostic value of that is.
    # Testing that a datetime could be parsed out at all is valuable, though.
    # It's worth the effort for the uptime though as that has a lot of diagnostic value.
    ##
    parsed_time = parse.get_current_system_time(bs)
    if parsed_time is None:
        log.error("Failed to parse modem datetime. HTML scrape error?")
        metrics.c_meta_parse_result.labels("datetime", False).inc()
    else:
        log.debug("Modem has datetime of", dt=parsed_time)
        metrics.c_meta_parse_result.labels("datetime", True).inc()

    # Parse out the up/down stream channel data
    log.debug("Attempting to parse downstream channel info from HTML...")
    downstream_channels_data = parse.extract_downstream_channels(bs)

    if downstream_channels_data is None:
        metrics.c_meta_parse_result.labels("conn_downstream", False).inc()
        return

    log.info(
        "Updating downstream channel metrics...", count=len(downstream_channels_data)
    )
    metrics.c_meta_parse_result.labels("conn_downstream", True).inc()
    _do_metrics_update(downstream_channels_data, metrics.DS_METRICS)

    log.debug("Attempting to parse upstream channel info from HTML...")
    upstream_channels_data = parse.extract_upstream_channels(bs)

    if upstream_channels_data is None:
        metrics.c_meta_parse_result.labels("conn_upstream", False).inc()
        return

    log.info("Updating upstream channel metrics...", count=len(upstream_channels_data))
    metrics.c_meta_parse_result.labels("conn_upstream", True).inc()
    _do_metrics_update(upstream_channels_data, metrics.US_METRICS)


def update_modem_metrics(prod_info_html: BeautifulSoup) -> None:
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
    if not isinstance(_uptime, timedelta):
        # In testing, most of the time, modem returns the full HTML and parsing uptime is easy.
        # But when the uptime is not present, the rest of the modem info also isn't present.
        # So if this fails, we can use it as a reliable proxy for the rest of the modem info.
        metrics.c_meta_parse_result.labels("product_info", False).inc()
        log.error(
            "Failed to parse product_info/uptime as timedelta. Scrape issue?",
            uptime=_uptime,
        )
    else:
        metrics.g_modem_uptime_seconds.set(_uptime.total_seconds())
        metrics.c_meta_parse_result.labels("product_info", True).inc()

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


def update_connection_metrics(connection_info_html: BeautifulSoup):
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

    # The first column is called "procedure"
    for procedure, data in startup_info.items():
        if procedure not in metrics.SU_METRICS:
            # TODO: increase a meta metric here; this is an indication that the HTML has changed
            #   (firmware update?!), scraper needs to be updated
            log.error(
                "Parsed startup procedure has no matching metric.",
                procedure=procedure,
                data=data,
            )
            continue
        # If the key points to None, we don't have a metric for it
        if (metric_data := metrics.SU_METRICS[procedure]) is not None:
            # TODO: instead of key into a dict, might make sense to just class MetricWrapper
            #   to have a single class that can validate and has dedicated / standard properties
            ##
            # metric_data contains the metric _and_ the associated possible values (if Enum)
            metric = metric_data["metric"]
            # Map between String and values the metric is expected to take
            values_map = metric_data["values"]
            # If we encounter a string that doesn't have a mapping, use this
            _default = metric_data["default"]

            if isinstance(metric, metrics.Gauge):
                # TODO: catch cast errors
                _v = data["Value"].split(" ")[0]
                metric.labels(data["Comment"]).set(float(_v))
            elif isinstance(metric, metrics.Enum):
                _v = values_map.get(data["Value"], _default)
                _c = data["Comment"] if data["Comment"] != "" else None
                metric.labels(_c).state(_v)
        # Not catching any errors here, yet so we only increment the True state... for not
        # TODO: obv, fix this ^
        metrics.c_meta_parse_result.labels("startup", True).inc()


# pylint: disable too-many-locals / R0914
# pylint: disable too-many-branches / R0912
def _do_metrics_update(
    # Each row is list of parsed strings
    ch_data: list[list[str]],
    # Map from column name to metric and optional flags
    metrics_map: OrderedDict[
        str, dict[str, str | None | metrics.Gauge | metrics.Summary | metrics.Counter]
    ],
):

    # See note in parse.py: it's not trivial to get the correct headers so we just index by position
    #   and hope for the best!
    # Need a copy of the column headers so we can figure out which column is the channel ID column.
    _headers = list(metrics_map.keys())

    if "Channel ID" not in _headers:
        log.error("Could not find 'Channel ID' in headers", headers=_headers)
        return
    # We know know which column index is the channel ID.
    ch_id_idx = _headers.index("Channel ID")

    # If the length of the row matches the length of headers, we can assume that the row was parsed
    #   without issue.
    # Ignore the position in the row that corresponds to the channel ID and the rest of the data should map
    #   to a specific metric.
    #
    # However some column's don't map directly to a specific metric. E.G: "Lock Status" is a column but we
    #   don't want to publish the lock status of each channel/row.
    # We want to publish the count of channels that is locked / not-locked as a gauge.
    # This means that for some metrics we'll need a "flags" field which indicates if we should use the
    #   simpler "set metric per channel_id/row" logic or if we need to do a cumulative sum across each
    #   row and then set the metric after we've walked the full table.
    ##
    # As of now, we only support one type of "flagged" metric
    _cumulative_sum_metrics = {}
    _cumulative_sum_metric_values = {}

    # Start by walking each row for sanity checking
    for idx, row in enumerate(ch_data):
        if len(row) != len(_headers):
            log.error(
                "Unexpected number of columns for row",
                row_idx=idx,
                expected_headers=_headers,
                data=row,
            )
            continue
        # Assuming check passes, each row should look like
        #   DS: ['4', 'Locked', 'QAM256', '363000000 Hz', '6.2 dBmV', '40.5 dB', '0', '0']
        #   US: ['1', '1', 'Locked', 'SC-QAM Upstream', '10400000 Hz', '3200000 Hz', '43.0 dBmV']
        ##
        # Extract the channel ID for the row; will need this for the simple "set per row" metrics
        _ch_id = int(row[ch_id_idx])
        log.debug("Processing row", row_idx=idx, channel_id=_ch_id)

        # If we made it this far, we know that the row is as long as the number of columns in the parsed table.
        # We can now walk across the row and process the value as appropriate
        for col_idx, metric_dict in enumerate(metrics_map.values()):
            # Any column that doesn't have a metric associated with it should be skipped
            if metric_dict is None or metric_dict["metric"] is None:
                continue
            _metric = metric_dict["metric"]
            # Likewise, the column that holds the channel ID should not be used as a metric
            if col_idx == ch_id_idx:
                continue

            # We can now assume that the position in the row corresponds to a raw value.
            # Before we can stuff that value into the metric, we need to clean it up so it can
            #   be cast to the correct type.
            # E.G.: a raw value of '363000000 Hz' needs to become just '363000000'
            #   so we can later int() or float() it.
            ##
            # Harmless if there's no space
            _tokens = row[col_idx].split(" ")
            _value = _tokens[0]

            # If this is not a simple "per-row" metric
            if metric_dict["flags"] is not None:
                # Store the metric by the column name
                _column_name = _headers[col_idx]
                if _column_name not in _cumulative_sum_metrics:
                    _cumulative_sum_metrics[_column_name] = _metric
                # And increase the count for the value
                if _column_name not in _cumulative_sum_metric_values:
                    _cumulative_sum_metric_values[_column_name] = {}

                if _value not in _cumulative_sum_metric_values[_column_name]:
                    _cumulative_sum_metric_values[_column_name][_value] = 1
                else:
                    _cumulative_sum_metric_values[_column_name][_value] += 1
                continue

            # Otherwise, this is a simple "per-row" metric
            log.debug(
                "Setting metric",
                channel=_ch_id,
                metric=_metric,
                value=_value,
                tokens=_tokens,
            )
            # the `metric` variable is a prometheus_client metric object and they all have different methods for setting values
            #   some use set(), some use observe() ... etc
            ##
            try:
                if isinstance(_metric, metrics.Gauge):
                    _metric.labels(channel_id=_ch_id).set(float(_value))
                elif isinstance(_metric, metrics.Summary):
                    _metric.labels(channel_id=_ch_id).observe(float(_value))
                elif isinstance(_metric, metrics.Counter):
                    # Keys into enum are CAP
                    _value = _value.upper()
                    # Likewise, any - symbols should be replaced with _ as `-` isn't permitted in enum key
                    _value = _value.replace("-", "_")
                    # TODO: This probably shouldn't be a counter; should be a gauge since we don't want to constantly increment
                    #   e.g.: if we have 32 channels locked, after 10 scrapes metric will be 320 which is not useful
                    # This means that we'll now have to re-factor this slightly as we can't just rely on metric type; not all Gauge will just get a .set()
                    #   call; some will need us to to counting _first_ and then set the value.
                    _metric.labels(_value).inc()
            # Catch cast errors
            except ValueError as ve:
                # TODO: meta metric increase here :D
                log.error(
                    "Failure to convert raw value into correct value for metric.",
                    row_idx=idx,
                    col_idx=col_idx,
                    metric=_metric,
                    _tokens=_tokens,
                    _value=_value,
                    error=ve,
                )
                continue
    # We have processed all rows and the per-row metrics.
    # If any cumulative metrics, we need to address them now.
    ##
    log.debug("Processing cumulative metrics", count=len(_cumulative_sum_metrics))
    # Walk the cumulative metrics
    for column_name, metric in _cumulative_sum_metrics.items():
        log.debug("Cumulative process", column=column_name, metric=metric)
        # Walk the unique value, count pairs for each metric
        for value_str, value_ct in _cumulative_sum_metric_values[column_name].items():
            metric.labels(value_str).set(float(value_ct))
