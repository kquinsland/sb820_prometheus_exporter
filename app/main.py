#!/usr/bin/env python3
"""
Main / entry point for SB family of modem exporter.

"""

import asyncio
from os import getenv
from time import time

import structlog
from aiohttp import ClientSession, ClientTimeout, CookieJar
from err.exceptions import ModemNotOkError, ModemUnauthorizedError, NoAuthTokenError
from prometheus_client import start_http_server
from sb8200 import metrics
from sb8200.parse import (
    is_login_page,
)
from sb8200.scrape import (
    do_login,
    do_modem_scrape,
    update_connection_channel_metrics,
    update_connection_metrics,
    update_modem_metrics,
)
from util.const import REQUEST_HEADERS, LogLevel

# cfg-file/arg-arse/clip is overkill for the few things that need to be configured.
# k8s makes it trivial to define env-vars so we'll just use that.
##
MODEM_BASE_URL = getenv("MODEM_BASE_URL", "https://192.168.100.1")

# support docs don't indicate that the username _can_ be changed
MODEM_USERNAME = getenv("MODEM_USERNAME", "admin")
# Password defaults to the last 8 digits of the SN; impossible to guess so require user provides
MODEM_PASSWORD = getenv("MODEM_PASSWORD", None)

# default prometheus_client implementation does not support setting the path, only the port.
METRICS_PORT = int(getenv("METRICS_PORT", "8200"))
METRICS_POLL_INTERVAL_SECONDS = int(getenv("METRICS_POLL_INTERVAL_SECONDS", "60"))
RE_LOGIN_INTERVAL_SECONDS = int(getenv("RE_LOGIN_INTERVAL_SECONDS", "5"))
# The modem intermittently 401s a login (see the ModemUnauthorizedError
# handler), so retry quickly a bounded number of times before falling back to
# the normal poll interval. Persistent failures remain non-fatal and visible in
# meta_scrape_success.
LOGIN_401_MAX_RETRIES = int(getenv("LOGIN_401_MAX_RETRIES", "2"))
MODEM_REQUEST_TIMEOUT_SECONDS = float(getenv("MODEM_REQUEST_TIMEOUT_SECONDS", "30"))


if getenv("LOG_LEVEL") not in LogLevel.__members__ or getenv("LOG_LEVEL") is None:
    print(f"Defaulting to {LogLevel.INFO} log level")
    log_level = LogLevel.INFO
else:
    log_level = LogLevel[getenv("LOG_LEVEL")]  # type: ignore
    print(f"Using log level {log_level.value}")


structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(log_level.value)
)

log = structlog.get_logger(__name__)


async def poll_modem(
    client: ClientSession, modem_username: str, modem_password: str
) -> None:
    """Poll forever while keeping transient modem failures non-fatal."""
    csrf_token = None
    login_401_retries = 0
    metrics.g_meta_scrape_success.set(0)

    while True:
        try:
            reuse_login = len(client.cookie_jar) > 0 and csrf_token is not None
            if not reuse_login:
                log.debug("Attempting to login.")
                csrf_token = await do_login(client, modem_username, modem_password)

            connection_html, prod_info_html = await do_modem_scrape(client, csrf_token)

            if is_login_page(connection_html) or is_login_page(prod_info_html):
                metrics.g_meta_scrape_success.set(0)
                csrf_token = None
                client.cookie_jar.clear()
                if reuse_login:
                    log.error("After skipping auth, Login page detected. Re-authing.")
                else:
                    # Often, the first login does not work so we have to login again
                    log.error(
                        "Login succeeded but stats page still returned Login page"
                    )
                    log.info(
                        f"Sleeping {RE_LOGIN_INTERVAL_SECONDS} seconds before next login"
                    )
                    await asyncio.sleep(RE_LOGIN_INTERVAL_SECONDS)
                continue

            # Made it past login and scrape, so reset the 401 retry budget.
            login_401_retries = 0

            parse_success = all(
                (
                    update_connection_metrics(connection_html),
                    update_connection_channel_metrics(connection_html),
                    update_modem_metrics(prod_info_html),
                )
            )
            metrics.g_meta_scrape_success.set(1 if parse_success else 0)
            if parse_success:
                metrics.g_meta_last_success_unixtime.set(time())
            else:
                log.error("Modem poll returned an incomplete or invalid snapshot")

            log.info(
                f"Sleeping {METRICS_POLL_INTERVAL_SECONDS} seconds before next poll"
            )
            await asyncio.sleep(METRICS_POLL_INTERVAL_SECONDS)

        except NoAuthTokenError as e:
            metrics.g_meta_scrape_success.set(0)
            log.error("Caught NoAuthTokenError", error=e)
            raise
        except ModemUnauthorizedError as e:
            # A 401 in the login request (cmconnectionstatus.html?login_<token>).
            # Two causes look identical at first:
            #   - wrong password: 401 every time.
            #   - a "cold" modem: after a reboot OR >10 min since the last successful
            #     login, it rejects the login even with correct credentials
            #   See re_notes/auth.md. Retry to ride out the cold case, but cap it
            #   so a persistent (bad-credentials) 401 surfaces. The counter
            #   resets on a successful login.
            metrics.g_meta_scrape_success.set(0)
            csrf_token = None
            client.cookie_jar.clear()
            if login_401_retries < LOGIN_401_MAX_RETRIES:
                login_401_retries += 1
                log.error(
                    "Retrying login after (possibly spurious) 401",
                    error=e,
                    attempt=login_401_retries,
                    max_attempts=LOGIN_401_MAX_RETRIES,
                )
                await asyncio.sleep(RE_LOGIN_INTERVAL_SECONDS)
                continue
            log.error(
                "Login failed after fast retries; will keep polling",
                error=e,
                attempt=login_401_retries,
            )
            await asyncio.sleep(METRICS_POLL_INTERVAL_SECONDS)
            continue
        except ModemNotOkError as e:
            metrics.g_meta_scrape_success.set(0)
            csrf_token = None
            client.cookie_jar.clear()
            log.error("Caught ModemNotOkError", error=e)
            await asyncio.sleep(METRICS_POLL_INTERVAL_SECONDS)
            continue
        # pylint: disable=broad-exception-caught
        except Exception as e:
            metrics.g_meta_scrape_success.set(0)
            csrf_token = None
            client.cookie_jar.clear()
            _e = "Unforeseen exception. Treating as non-fatal."
            log.error(_e, error=e)
            await asyncio.sleep(METRICS_POLL_INTERVAL_SECONDS)
            continue


async def main() -> None:
    """Main entry point."""
    log.info("Starting up")
    modem_username = MODEM_USERNAME
    modem_password = MODEM_PASSWORD
    if modem_username is None or modem_password is None:
        raise NoAuthTokenError("Missing MODEM_USERNAME or MODEM_PASSWORD")

    # In testing, server responds to requests on / and /metrics so there's no real
    # need to allow customizing the path.
    server, _ = start_http_server(port=METRICS_PORT)
    log.info("Metrics server started", server=server.server_address)

    log.debug("Setting up connection to modem...")
    timeout = ClientTimeout(total=MODEM_REQUEST_TIMEOUT_SECONDS)
    async with ClientSession(
        base_url=MODEM_BASE_URL,
        headers=REQUEST_HEADERS,
        timeout=timeout,
        # unsafe=True: tell aiohttp to allow cookies on IP addresses
        cookie_jar=CookieJar(unsafe=True),
    ) as client:
        await poll_modem(client, modem_username, modem_password)


if __name__ == "__main__":
    asyncio.run(main())
