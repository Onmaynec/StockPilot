from app.perf_metrics import resource_delta, resource_snapshot


def test_resource_snapshot_is_best_effort():
    snapshot = resource_snapshot()
    assert snapshot is None or set(snapshot) == {
        "cpuUsageUsec", "cpuQuotaCores", "memoryCurrentBytes", "memoryPeakBytes"
    }
    if snapshot is not None:
        assert all(value is None or isinstance(value, int | float)
                   for value in snapshot.values())


def test_resource_delta_marks_missing_cgroup_metrics_unavailable():
    assert resource_delta(None, None, 1.0) == {"available": False}


def test_resource_delta_reports_cpu_and_memory_without_guessing():
    before = {
        "cpuUsageUsec": 1_000_000,
        "cpuQuotaCores": 1.0,
        "memoryCurrentBytes": 10 * 1024 * 1024,
        "memoryPeakBytes": 20 * 1024 * 1024,
    }
    after = {
        "cpuUsageUsec": 2_000_000,
        "cpuQuotaCores": 1.0,
        "memoryCurrentBytes": 20 * 1024 * 1024,
        "memoryPeakBytes": 30 * 1024 * 1024,
    }
    result = resource_delta(before, after, 2.0)
    assert result["cpuTimeSeconds"] == 1.0
    assert result["cpuEquivalentCores"] == 0.5
    assert result["cpuPercentOfQuota"] == 50.0
    assert result["memoryCurrentMiB"] == 20.0
    assert result["containerMemoryPeakMiB"] == 30.0
    assert result["memoryDeltaMiB"] == 10.0
