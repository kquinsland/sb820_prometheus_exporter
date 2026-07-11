"""Regression checks for PromQL expressions shipped with the dashboard."""

import json
from pathlib import Path


def _expressions(value):
    if isinstance(value, dict):
        if "expr" in value:
            yield value["expr"]
        for child in value.values():
            yield from _expressions(child)
    elif isinstance(value, list):
        for child in value:
            yield from _expressions(child)


def test_dashboard_uses_aggregated_ratios_and_real_codeword_counters():
    dashboard = json.loads(
        Path("dashboards/sb-exporter.json").read_text(encoding="utf-8")
    )
    expressions = list(_expressions(dashboard))

    assert not any("downstream_corrected_sum" in expr for expr in expressions)
    assert not any("downstream_uncorrectable_sum" in expr for expr in expressions)
    assert any("sb8200_downstream_corrected_total" in expr for expr in expressions)
    assert any("sb8200_downstream_uncorrectable_total" in expr for expr in expressions)

    downstream_lock = next(
        expr
        for expr in expressions
        if "sb8200_downstream_channel_lock_status_count" in expr
    )
    upstream_lock = next(
        expr for expr in expressions if "sb8200_upstream_channel_lock_count" in expr
    )
    scrape_success = next(expr for expr in expressions if "meta_scrape_success" in expr)

    assert "sum(" in downstream_lock and "or vector(0)" in downstream_lock
    assert "sum(" in upstream_lock and "or vector(0)" in upstream_lock
    assert scrape_success == 'meta_scrape_success{instance=~"$exporter_instance"}'
    assert not any("meta_scrape_result_total" in expr for expr in expressions)
