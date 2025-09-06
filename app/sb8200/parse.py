"""
Some generic parsing functions that pull out the data we're interested in from the HTML source.
    Only tested against SB8200's web interface but should work for other Arris modems as well.

"""

import re
from datetime import datetime, timedelta

import structlog
from bs4 import BeautifulSoup

log = structlog.get_logger(__name__)


def is_login_page(soup: BeautifulSoup) -> bool:
    """Check if the page is the login page"""
    return soup.find("title").text.strip() == "Login"


def get_modem_info(soup: BeautifulSoup) -> dict[str, None | str | timedelta]:
    """Attempts to extract some modem information from the product-info page.

    - Software Version: nice to graph over time to see if/when a firmware update is pushed out
    - Cable Modem MAC Address: this should never change; mostly useful for Info() type metric
    - Serial Number: Like MAC, should never change but nice to have for Info() type metric
    - Up Time: nice to graph over time to see if/when the modem has been rebooted.
    """

    _modem_info = {
        "docsis_version": None | str,
        "software_version": None | str,
        "mac_address": None | str,
        "serial_number": None | str,
        "up_time": None | timedelta,
    }
    # Since we're scraping HTML, we have to be careful about the structure of the page and check that the various find() calls
    #   return something before trying to access the .text attribute
    ##
    # Software version
    if (sv_row := soup.find("td", text="Standard Specification Compliant")) is not None:
        if (ver := sv_row.find_next_sibling("td")) is not None:
            _modem_info["docsis_version"] = ver.text.strip()

    # Software version
    if (sv_row := soup.find("td", text="Software Version")) is not None:
        if (ver := sv_row.find_next_sibling("td")) is not None:
            _modem_info["software_version"] = ver.text.strip()

    # Cable Modem MAC Address
    if (mac_row := soup.find("td", text="Cable Modem MAC Address")) is not None:
        if (mac := mac_row.find_next_sibling("td")) is not None:
            _modem_info["mac_address"] = mac.text.strip()

    # Serial Number
    if (sn_row := soup.find("td", text="Serial Number")) is not None:
        if (sn := sn_row.find_next_sibling("td")) is not None:
            _modem_info["serial_number"] = sn.text.strip()

    # Uptime
    if (ut_row := soup.find("td", text="Up Time")) is not None:
        if (ut := ut_row.find_next_sibling("td")) is not None:
            _modem_info["up_time"] = _get_uptime_str_as_timedelta(ut.text.strip())

    log.debug(
        "Modem Info",
        docsis_version=_modem_info["docsis_version"],
        version=_modem_info["software_version"],
        mac=_modem_info["mac_address"],
        sn=_modem_info["serial_number"],
        ut=_modem_info["up_time"],
    )
    return _modem_info


def get_current_system_time(soup: BeautifulSoup) -> datetime | None:
    """Attempts to pull the current system time from the HTML source."""
    # Towards the very end of the page is a single <p> tag with the system time
    # <p id="systime" align="center"><strong>Current System Time:</strong> Tue Mar 12 14:20:59 2024</p>
    if (system_time_row := soup.find("p", id="systime")) is not None:
        system_time_str = system_time_row.text.strip()
        # The format is "Day Mon dd hh:mm:ss yyyy"
        datetime_str = system_time_str.split(":", 1)[1].strip()
        return datetime.strptime(datetime_str, "%a %b %d %H:%M:%S %Y")

    log.warning("Failed to find system time. Scrape error?")
    return None


def extract_startup_procedure(soup: BeautifulSoup) -> dict[str, dict[str, str]]:
    """Pull startup procedure data from the HTML source of the SB8200's web interface"""
    # Find the table with the "Startup Procedure" header
    tables = soup.find_all("table", class_="simpleTable")
    for table in tables:
        headers = table.find_all("th")
        # Check if one of the headers contains the text "Startup Procedure"
        if any("Startup Procedure" in header.text for header in headers):
            rows = table.find_all("tr")
            # Extract the key, value, and comment for specified rows
            startup_procedure_data = {}
            for row in rows:
                cols = row.find_all("td")
                if len(cols) == 3:  # Ensure row has key, value, and comment
                    key = cols[0].text.strip()
                    value = cols[1].text.strip()
                    comment = cols[2].text.strip()
                    # Store the data if key matches the specified rows
                    # Makes it easier to identify data that was missing or otherwise not as expected
                    if key in [
                        "Acquire Downstream Channel",
                        "Connectivity State",
                        "Boot State",
                        "Configuration File",
                        "Security",
                        "DOCSIS Network Access Enabled",
                    ]:
                        startup_procedure_data[key] = {
                            "Value": value,
                            "Comment": comment,
                        }
            return startup_procedure_data
    return {}


# Of course the modem returns INVALID html.
# Here's a snippet of the HTML that we're trying to parse:
#                   <tr>
#                     <th colspan=8><strong>Downstream Bonded Channels</strong></th>
#                   </tr>
#                   <td><strong>Channel ID</strong></td>
#                   <td><strong>Lock Status</strong></td>
#                   <td><strong>Modulation</strong></td>
#                   <td><strong>Frequency</strong></td>
#                   <td><strong>Power</strong></td>
#                   <td><strong>SNR/MER</strong></td>
#                   <td><strong>Corrected</strong></td>
#                   <td><strong>Uncorrectables</strong></td>
#                   </tr>
#
# Notice that the opening `<tr>` is closed ... TWICE?
# Yeah, BeautifulSoup doesn't like that.
# This makes it difficult to extract the headings in addition to the data.
#
# While the more correct thing would be to properly parse out the headings dynamically so we can
#     handle any changes to the HTML structure, I'm going to take the lazy way out and just hardcode
#     the headings for now.
# I'm not expecting the headings to change often, so this should be fine for now.


# Define a function to extract table rows
def _extract_table_rows(
    soup: BeautifulSoup, table_section_title: str
) -> list[list[str]] | None:
    tables = soup.find_all("table", class_="simpleTable")
    log.debug("Tables", title=table_section_title, count=len(tables))
    for table in tables:
        # Check if the current table contains the section title
        if table.find("th", string=table_section_title):
            rows = table.find_all("tr")
            log.debug("Rows", title=table_section_title, count=len(rows))
            data_rows = []
            # See note above about the weird HTML structure, we skip the first row
            for row in rows[1:]:
                cols = row.find_all("td")
                row_data = [col.text.strip() for col in cols]
                data_rows.append(row_data)
            return data_rows
    return None


def extract_downstream_channels(soup: BeautifulSoup) -> list[list[str]] | None:
    """Wrapper"""
    return _extract_table_rows(soup, "Downstream Bonded Channels")


def extract_upstream_channels(soup: BeautifulSoup) -> list[list[str]] | None:
    """Wrapper"""
    return _extract_table_rows(soup, "Upstream Bonded Channels")


def _get_uptime_str_as_timedelta(uptime: str) -> timedelta | None:
    """
    Turns the human-friendly string into a timedelta object

    Input ends up being something like:
        '0 days 00h:01m:55s.00'
            or
        '46 days 12h:55m:21s.00'
    """
    match = re.match(r"(\d+) days (\d+)h:(\d+)m:(\d+)s", uptime)
    if match:
        days, hours, minutes, seconds = map(int, match.groups())
        # Creating a timedelta object for the duration
        up_time_delta = timedelta(
            days=days, hours=hours, minutes=minutes, seconds=seconds
        )
    else:
        up_time_delta = None

    return up_time_delta
