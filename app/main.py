#!/usr/bin/env python3
"""
Main / entry point for SB family of modem exporter.

"""
import asyncio
from os import getenv

import structlog
from aiohttp import BasicAuth, ClientSession
from err.exceptions import ModemNotOkError, NoAuthTokenError
from prometheus_client import start_http_server
from sb8200.scrape import (
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


if getenv("LOG_LEVEL") not in LogLevel.__members__ or getenv("LOG_LEVEL") is None:
    print(f"Defaulting to {LogLevel.INFO} log level")
    log_level = LogLevel.INFO
else:
    print(f"Using log level {LogLevel.INFO}")
    log_level = LogLevel[getenv("LOG_LEVEL")]  # type: ignore

structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(log_level.value)
)

log = structlog.get_logger(__name__)


async def main():
    """Main entry point."""
    log.info("Starting up")
    # Check that user set auth
    if MODEM_USERNAME is None or MODEM_PASSWORD is None:
        log.error("Missing MODEM_USERNAME or MODEM_PASSWORD")
        return

    # In testing, server responds to requests on / and /metrics so there's no real
    #   need to allow customizing the path, I think.
    server, _ = start_http_server(port=METRICS_PORT)
    log.info("Metrics server started", server=server.server_address)

    log.debug("Setting up connection to modem...")
    client = ClientSession(
        auth=BasicAuth(MODEM_USERNAME, MODEM_PASSWORD),
        base_url=MODEM_BASE_URL,
        headers=REQUEST_HEADERS,
    )

    while True:

        try:
            connection_html, prod_info_html = await do_modem_scrape(client)

            # High level connection info
            update_connection_metrics(connection_html)
            # Channel specific
            update_connection_channel_metrics(connection_html)
            # Misc things like firmware version, uptime, etc.
            update_modem_metrics(prod_info_html)

            log.info(
                f"Sleeping {METRICS_POLL_INTERVAL_SECONDS} seconds before next poll"
            )
            await asyncio.sleep(METRICS_POLL_INTERVAL_SECONDS)

        except NoAuthTokenError as e:
            # Something went wrong in early auth/login. Can't continue.
            # Bail and wait until next cycle.
            log.error("Caught NoAuthTokenError", error=e)
            break
        except ModemNotOkError as e:
            # Got a non 200/OK back from the modem.
            # In testing, I could only ever get a non 200/OK from
            #   auth related issues.
            # In any case, log, bail and wait until the next cycle
            log.error("Caught ModemNotOkError", error=e)
            break
        # pylint: disable=broad-exception-caught
        except Exception as e:
            _e = "Unforeseen exception. Treating as non-fatal."
            log.error(_e, error=e)
            continue


if __name__ == "__main__":
    asyncio.run(main())
