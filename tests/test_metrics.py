"""Prometheus metrics: the counter registry and text exposition."""

from sentinel.metrics import COUNTERS, Metrics, render


def test_inc_and_snapshot():
    m = Metrics()
    assert m.snapshot()["dispatches_total"] == 0
    m.inc("dispatches_total")
    m.inc("dispatches_total", 2)
    assert m.snapshot()["dispatches_total"] == 3


def test_unknown_counter_is_ignored():
    m = Metrics()
    m.inc("not_a_real_counter")            # must not raise, must not appear
    assert "not_a_real_counter" not in m.snapshot()


def test_render_emits_counters_with_help_and_type():
    m = Metrics()
    m.inc("escalations_total", 4)
    out = render(m.snapshot(), gauges={})
    assert "# HELP sentinel_escalations_total" in out
    assert "# TYPE sentinel_escalations_total counter" in out
    assert "sentinel_escalations_total 4" in out
    # every declared counter is exposed, even at zero
    for name in COUNTERS:
        assert f"sentinel_{name} " in out


def test_render_includes_gauges():
    out = render(Metrics().snapshot(),
                 gauges={"paused": ("1 when paused.", 1),
                         "running_agents": ("agents", 3)})
    assert "# TYPE sentinel_paused gauge" in out
    assert "sentinel_paused 1" in out
    assert "sentinel_running_agents 3" in out


def test_render_is_valid_exposition_shape():
    out = render(Metrics().snapshot(), gauges={"up": ("up", 1)})
    assert out.endswith("\n")
    for line in out.splitlines():
        assert line.startswith("#") or line.startswith("sentinel_")
