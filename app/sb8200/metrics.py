"""All the boiler plate / init code for defining metrics.
Most metrics are derived from one or more of the fields in parsed HTML tables.
Where needed, the metric + additional data is wrapped in dict{} and associated with the HTML column header
    that the data was pulled from.

OrderedDict is used to associate the column header / position in the row with the specific metric that should be updated.
"""

from collections import OrderedDict

from prometheus_client import (Counter, Enum, Gauge, Info, Summary,
                               disable_created_metrics)

# By default, client will automatically create a "_created" meta metric for
#   each metric defined below.
# Having the unix epoch time of when the metric was created isn't that useful for us
#   so we'll disable it.
disable_created_metrics()


# TODO: this should be cli arg or env var
METRICS_NS = "sb8200"
META_NS = "meta"

# In an attempt to make this script more resilient to changes in the HTML structure, we're going to
#   build a thruple that maps a column header to the position in the row and the metric that the value
#   should be stuffed into.
# This way, if the HTML changes, we only need to update the thruple and not the rest of the code.
# Also comes in handy since the list of headers is different for upstream vs downstream.
##
DS_METRICS = OrderedDict()
US_METRICS = OrderedDict()

# Startup data
SU_METRICS = OrderedDict()

# On the "downstream" table, the first column is "Channel ID" so we make sure
#   it's the first key in the dict.
# There is no metric associated with this key as the data we pull from the channel id
#   position in each row will be used as a label for the other metrics
##
DS_METRICS["Channel ID"] = None

##
# Meta Metrics
##
# How long are we spending on scrapes?
# Assuming the reply is nominally consistent in size, we should expect pretty consistent process times
#   so we're not going to instrument the html process time.
# I'm interested in any change in how long the modem takes to reply, though.
##
# summary comes with both a count and a sum so we don't need to count the number of scrapes ourselves
s_meta_scrape_time = Summary(
    f"{META_NS}_request_duration_seconds",
    "Time spent waiting for modem to respond",
    # We only scrape a few pages so we can index by the page
    labelnames=["scrape_target"],
)

# We count the number of successful vs failed scrapes for each page we scrape
c_meta_scrape_result = Counter(
    f"{META_NS}_scrape_result",
    "Count of successful vs failed scrapes",
    # Scraping by a few pages and possible HTTP codes is bounded (i've only ever seen 200/401)
    #   so we're not going to blow up storage by doing this.
    labelnames=["http_code", "scrape_target"],
)

# Time to parse returned HTML isn't interesting but am interested in parse errors
c_meta_parse_result = Counter(
    f"{META_NS}_parse_result",
    "Count of successful vs failed parse attempts",
    # Only a few blocks of HTML that we're attempting to parse
    labelnames=["parse_target", "parse_result"],
)

##
# General hardware info / metrics
##
# Info() is perfect for key/value pairs that are not expected to change often.
i_modem_info = Info(
    f"{METRICS_NS}_modem",
    "Assorted Modem Info",
)

g_modem_uptime_seconds = Gauge(
    f"{METRICS_NS}_modem_uptime_seconds",
    "Count of seconds since modem was last booted.",
)


##
# High level connection metrics
##
# Presumably you can't have a frequency and also be not-locked so just use a gauge for the frequency
# metric=Acquire Downstream Channel value={'Value': '363000000 Hz', 'Comment': 'Locked'}
g_startup_downstream_channel_hz = Gauge(
    f"{METRICS_NS}_startup_downstream_channel_hz",
    "Initial frequency of the downstream channel.",
    labelnames=["comment"],
)
SU_METRICS["Acquire Downstream Channel"] = {
    "metric": g_startup_downstream_channel_hz,
    "values": None,
    "default": None,
}


# metric=Connectivity State value={'Value': 'OK', 'Comment': 'Operational'}
e_startup_connectivity_state = Enum(
    f"{METRICS_NS}_startup_connectivity_state",
    "Indicates if DOCSIS network detected.",
    states=["ok", "not_ok"],
    labelnames=["comment"],
)
SU_METRICS["Connectivity State"] = {
    "metric": e_startup_connectivity_state,
    "values": {
        "OK": "ok",
    },
    "default": "not_ok",
}

# I'm not really sure what value(s) this particular line item is meant to display.
# What possible values for "boot state" are there that we'd also be able to scrape?
# Not going to bother with a metric for this one
##

# metric=Boot State value={'Value': 'OK', 'Comment': 'Operational'}
SU_METRICS["Boot State"] = None

# metric=Configuration File value={'Value': 'OK', 'Comment': ''}
e_startup_config_file_state = Enum(
    f"{METRICS_NS}_startup_config_state",
    "Indicates if head-end provisioning file is valid.",
    states=["ok", "not_ok"],
    labelnames=["comment"],
)
SU_METRICS["Configuration File"] = {
    "metric": e_startup_config_file_state,
    "values": {
        "OK": "ok",
    },
    "default": "not_ok",
}

# metric=Security value={'Value': 'Enabled', 'Comment': 'BPI+'}
e_startup_security_state = Enum(
    f"{METRICS_NS}_startup_security_state",
    "Indicates if DOCSIS layer 1/2 security in effect.",
    # TODO: this should be an enum in a const.py
    states=["enabled", "not_enabled"],
    labelnames=["comment"],
)
SU_METRICS["Security"] = {
    "metric": e_startup_security_state,
    "values": {
        "Enabled": "enabled",
    },
    "default": "not_enabled",
}


# metric=DOCSIS Network Access Enabled value={'Value': 'Allowed', 'Comment': ''}
e_startup_docsis_net_access = Enum(
    f"{METRICS_NS}_startup_docsis_net_access_state",
    "Indicates if the modem is authorized to access the DOCSIS network.",
    # TODO: this should be an enum in a const.py
    states=["allowed", "not_allowed"],
    labelnames=["comment"],
)
SU_METRICS["DOCSIS Network Access Enabled"] = {
    "metric": e_startup_docsis_net_access,
    "values": {
        "Allowed": "allowed",
    },
    "default": "not_allowed",
}


##
# Channel specific metrics
##

# While technically possible to also index on the channel ID, it's expensive to have
#   channel_id as a label. Can get almost all of the value w/ significantly lower storage
#   cost by just having a count of locked vs not locked channels.
# If the count of locked vs not locked channels changes often then maybe able to justify
#   the extra storage cost of having channel_id as a label.
##
g_downstream_lock_status = Gauge(
    f"{METRICS_NS}_downstream_channel_lock_status_count",
    "Lock/Not Lock status of the channel.",
    labelnames=["lock_status"],
)
DS_METRICS["Lock Status"] = {"metric": g_downstream_lock_status, "flags": "cumulative"}

# Same reasoning as channel lock status; not really worth it to index by channel_id
g_downstream_modulation_scheme = Gauge(
    f"{METRICS_NS}_downstream_modulation_scheme_count",
    "Count of modulation scheme of the channel.",
    labelnames=["modulation_scheme"],
)
DS_METRICS["Modulation"] = {
    "metric": g_downstream_modulation_scheme,
    "flags": "cumulative",
}

##
# Most of what's interesting here is the up/downstream channels
# For each channel we have the following data about it
#   ChannelID, LockStatus, Modulation, Frequency, Power, SNR/MER, Corrected, Uncorrectables
# We'll make a metric for each of these and index them by ChannelID

g_downstream_frq_hz = Gauge(
    f"{METRICS_NS}_downstream_frequency_hz",
    "Frequency of the channel.",
    labelnames=["channel_id"],
)
DS_METRICS["Frequency"] = {"metric": g_downstream_frq_hz, "flags": None}


g_downstream_power_db = Gauge(
    f"{METRICS_NS}_downstream_power_dbmv",
    "Received power of the channel.",
    labelnames=["channel_id"],
)
DS_METRICS["Power"] = {"metric": g_downstream_power_db, "flags": None}


g_downstream_snr_db = Gauge(
    f"{METRICS_NS}_downstream_snr_dbmv",
    "Signal to Noise Ratio / Modulation Error Ratio of the channel.",
    labelnames=["channel_id"],
)
DS_METRICS["SNR/MER"] = {"metric": g_downstream_snr_db, "flags": None}


s_downstream_corrected = Summary(
    f"{METRICS_NS}_downstream_corrected",
    "Count of corrected errors on the channel.",
    labelnames=["channel_id"],
)
DS_METRICS["Corrected"] = {"metric": s_downstream_corrected, "flags": None}


s_downstream_uncorrectable = Summary(
    f"{METRICS_NS}_downstream_uncorrectable",
    "Count of uncorrectable errors on the channel.",
    labelnames=["channel_id"],
)
DS_METRICS["Uncorrectables"] = {"metric": s_downstream_uncorrectable, "flags": None}

##
# Upstream metrics
##
# The first column is "Channel" which appears to just be a numerical index the same way a spreadsheet would
#   have a number for each row. We can ignore this column.
US_METRICS["Channel"] = None
# The second column is "Channel ID" which is the same as the downstream channel id; it's just a label for the rest of the data.
US_METRICS["Channel ID"] = None

g_upstream_lock_count = Gauge(
    f"{METRICS_NS}_upstream_channel_lock_count",
    "Lock/Not Lock status of the channel.",
    labelnames=["lock_status"],
)
US_METRICS["Lock Status"] = {"metric": g_upstream_lock_count, "flags": "cumulative"}


# This appears to be another name for modulation scheme?
g_upstream_channel_type = Gauge(
    f"{METRICS_NS}_upstream_channel_type",
    "Modulation scheme of the channel.",
    labelnames=["modulation_scheme"],
)
US_METRICS["US Channel Type"] = {
    "metric": g_upstream_channel_type,
    "flags": "cumulative",
}

# I am not a DOCSIS expert but I think this is the carrier/middle frequency of the channel?
g_upstream_frq_hz = Gauge(
    f"{METRICS_NS}_upstream_frequency_hz",
    "Frequency of the upstream channel.",
    labelnames=["channel_id"],
)
US_METRICS["Frequency"] = {"metric": g_upstream_frq_hz, "flags": None}

# Width of the channel
g_upstream_ch_width_hz = Gauge(
    f"{METRICS_NS}_upstream_ch_width_hz",
    "Width of upstream channel.",
    labelnames=["channel_id"],
)
US_METRICS["Width"] = {"metric": g_upstream_ch_width_hz, "flags": None}

# Transmit power of the channel
g_upstream_power_db = Gauge(
    f"{METRICS_NS}_upstream_power_dbmv",
    "Transmit power of the channel.",
    labelnames=["channel_id"],
)
US_METRICS["Power"] = {"metric": g_upstream_power_db, "flags": None}
