"""Exercise startup configuration in isolated metric registries/processes."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_metrics(namespace, script):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "app"))
    env.pop("METRICS_NAMESPACE", None)
    if namespace is not None:
        env["METRICS_NAMESPACE"] = namespace
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


@pytest.mark.parametrize("namespace", [None, "custom_modem"])
def test_namespace_preserves_scraping_and_state_labels(namespace):
    result = run_metrics(
        namespace,
        """
from pathlib import Path
from bs4 import BeautifulSoup
from prometheus_client import REGISTRY
from sb8200 import metrics, scrape

prefix = metrics.METRICS_NS
for fixture, security, access in [
    ('normal', 'enabled', 'allowed'),
    ('outage', 'not_enabled', 'not_allowed'),
    ('normal', 'enabled', 'allowed'),
]:
    html = Path(f'tests/fixtures/cmconnectionstatus_{fixture}.html').read_text()
    assert scrape.update_connection_metrics(BeautifulSoup(html, 'html.parser'))
    for suffix, state, comment in [
        ('startup_security_state', security, 'BPI+'),
        ('startup_docsis_net_access_state', access, ''),
    ]:
        name = f'{prefix}_{suffix}'
        assert REGISTRY.get_sample_value(name, {name: state, 'comment': comment}) == 1
assert REGISTRY.get_sample_value('meta_scrape_success') is not None
names = {metric.name for metric in REGISTRY.collect()}
assert f'{prefix}_modem_uptime_seconds' in names
if prefix != 'sb8200':
    assert not any(name.startswith('sb8200_') for name in names)
""",
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "namespace", ["", "bad-prefix", "123", "has space", "has:colon"]
)
def test_invalid_namespace_fails_at_startup(namespace):
    result = run_metrics(namespace, "import sb8200.metrics")
    assert result.returncode != 0
    assert (
        "METRICS_NAMESPACE must be a nonempty Prometheus metric prefix" in result.stderr
    )
