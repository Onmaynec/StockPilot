"""Portable cgroup v2 resource snapshots for reproducible performance reports."""
import os


def resource_snapshot():
    """Return best-effort cgroup metrics; unavailable fields remain None."""
    result = {"cpuUsageUsec": None, "cpuQuotaCores": None,
              "memoryCurrentBytes": None, "memoryPeakBytes": None}
    try:
        with open("/sys/fs/cgroup/cpu.stat", encoding="ascii") as stream:
            for line in stream:
                parts = line.split()
                if len(parts) == 2 and parts[0] == "usage_usec":
                    result["cpuUsageUsec"] = int(parts[1])
                    break
    except (OSError, ValueError):
        pass
    try:
        with open("/sys/fs/cgroup/cpu.max", encoding="ascii") as stream:
            quota, period = stream.read().split()
            if quota != "max":
                result["cpuQuotaCores"] = round(int(quota) / int(period), 3)
    except (OSError, ValueError, ZeroDivisionError):
        pass
    for filename, key in (("memory.current", "memoryCurrentBytes"),
                          ("memory.peak", "memoryPeakBytes")):
        try:
            with open("/sys/fs/cgroup/" + filename, encoding="ascii") as stream:
                result[key] = int(stream.read().strip())
        except (OSError, ValueError):
            pass
    return result if any(value is not None for value in result.values()) else None


def resource_delta(before, after, elapsed_seconds):
    """Summarize CPU time and memory observations without inventing missing data."""
    if not before or not after:
        return {"available": False}
    result = {"available": True}
    old, new = before.get("cpuUsageUsec"), after.get("cpuUsageUsec")
    if old is not None and new is not None:
        cpu_seconds = max(0, new - old) / 1_000_000
        equivalent = cpu_seconds / max(elapsed_seconds, 0.001)
        quota = after.get("cpuQuotaCores")
        result.update({
            "cpuTimeSeconds": round(cpu_seconds, 3),
            "cpuEquivalentCores": round(equivalent, 3),
            "cpuQuotaCores": quota,
            "cpuPercentOfQuota": round(equivalent / quota * 100, 2) if quota else None,
        })
    else:
        result.update({"cpuTimeSeconds": None, "cpuEquivalentCores": None,
                       "cpuQuotaCores": after.get("cpuQuotaCores"),
                       "cpuPercentOfQuota": None})
    for source, name in (("memoryCurrentBytes", "memoryCurrentMiB"),
                         ("memoryPeakBytes", "containerMemoryPeakMiB")):
        value = after.get(source)
        result[name] = round(value / 1024 / 1024, 2) if value is not None else None
    old_mem, new_mem = before.get("memoryCurrentBytes"), after.get("memoryCurrentBytes")
    result["memoryDeltaMiB"] = (
        round((new_mem - old_mem) / 1024 / 1024, 2)
        if old_mem is not None and new_mem is not None else None
    )
    result["hostCpuCount"] = os.cpu_count()
    return result
