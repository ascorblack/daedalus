"""What the machine's workloads cost it — terminals and browsers — and what a cap would cost if filled.

The daemons measure every running terminal's and every browser's process tree every ten seconds
(memory, CPU). Nothing of that is stored per terminal or per browser: the host keeps, per *profile* —
``shell``, the CLI a harness runs, or ``browser`` — the average cost of one of that kind over its
recent samples, because that is the number the question "what would twenty of them cost" needs, and
it outlives what it was measured on. The projection is arithmetic on it and is kept apart from the
tracking, so it can be tested with numbers rather than processes. It lives outside both packages
because both use it and neither owns it.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

WINDOW = 60
"""Samples kept per profile: ten minutes of a terminal that runs, at one sample per ten seconds."""

DEFAULT_COST = {"rss_bytes": 64 << 20, "cpu_percent": 1.0}
"""What a terminal of a profile never measured is assumed to cost: a shell with a small program in it.
A guess, said as one in the response, and replaced by the first real sample."""

WARN_PERCENT = 70.0
BAD_PERCENT = 90.0


@dataclass(frozen=True, slots=True)
class Cost:
    rss_bytes: float
    cpu_percent: float
    """Of one CPU, as the daemon reports a process tree's use."""
    samples: int

    def view(self) -> dict[str, Any]:
        return {"rss_bytes": int(self.rss_bytes), "cpu_percent": round(self.cpu_percent, 1), "samples": self.samples}


class ProfileCosts:
    """The rolling average cost of one terminal, per profile."""

    def __init__(self, window: int = WINDOW) -> None:
        self.window = window
        self._samples: dict[str, deque[tuple[float, float]]] = {}

    def add(self, profile: str, rss_bytes: float, cpu_percent: float) -> None:
        bucket = self._samples.get(profile)
        if bucket is None:
            bucket = self._samples[profile] = deque(maxlen=self.window)
        bucket.append((max(0.0, float(rss_bytes)), max(0.0, float(cpu_percent))))

    def cost(self, profile: str) -> Cost | None:
        bucket = self._samples.get(profile)
        if not bucket:
            return None
        return Cost(sum(r for r, _ in bucket) / len(bucket), sum(c for _, c in bucket) / len(bucket), len(bucket))

    def profiles(self) -> dict[str, Cost]:
        return {name: cost for name in sorted(self._samples) if (cost := self.cost(name)) is not None}

    def dump(self) -> dict[str, list[list[float]]]:
        return {name: [list(s) for s in bucket] for name, bucket in self._samples.items()}

    def load(self, data: Mapping[str, Any]) -> None:
        """Restore what :meth:`dump` wrote; anything malformed is skipped, it is only an estimate."""
        for name, samples in data.items():
            if not isinstance(name, str) or not isinstance(samples, list):
                continue
            for sample in samples[-self.window :]:
                if isinstance(sample, list) and len(sample) == 2 and all(isinstance(v, int | float) for v in sample):
                    self.add(name, sample[0], sample[1])


def likely_cost(costs: Mapping[str, Cost], running_profiles: Iterable[str]) -> tuple[Cost, str]:
    """The average cost of the next terminal, and what it was based on.

    The terminals that run now are the best guess of what the next ones will be — a machine running
    twelve Claude sessions will open a thirteenth, not a shell — so the mix of running profiles
    weights the average. With nothing running, every profile ever measured counts once; with nothing
    measured, the default guess.
    """
    running = [p for p in running_profiles]
    weighted = [(costs[p], 1) for p in running if p in costs]
    basis = "running"
    if not weighted:
        weighted = [(c, 1) for c in costs.values()]
        basis = "measured"
    if not weighted:
        return Cost(DEFAULT_COST["rss_bytes"], DEFAULT_COST["cpu_percent"], 0), "default"
    total = sum(w for _, w in weighted)
    return Cost(sum(c.rss_bytes * w for c, w in weighted) / total, sum(c.cpu_percent * w for c, w in weighted) / total, sum(c.samples for c, _ in weighted)), basis


def level(percent: float) -> str:
    return "bad" if percent > BAD_PERCENT else "warn" if percent > WARN_PERCENT else "ok"


def effective_memory(machine: Mapping[str, Any]) -> tuple[int, int]:
    """``(total, available)`` bytes of the place the terminals run, a container's limit included.

    A container with a memory limit is out of memory at its limit, whatever the machine around it has
    free, so the smaller of the two answers is the true one.
    """
    total = int(machine.get("mem_total_bytes") or 0)
    available = int(machine.get("mem_available_bytes") or 0)
    limit = int(machine.get("cgroup_limit_bytes") or 0)
    if limit and (not total or limit < total):
        used = int(machine.get("cgroup_used_bytes") or 0)
        total = limit
        available = min(available, max(0, limit - used)) if available else max(0, limit - used)
    return total, available


def effective_cpus(machine: Mapping[str, Any]) -> float:
    cpus = float(machine.get("cpus") or 0)
    quota = float(machine.get("cgroup_cpus") or 0)
    return quota if quota and (not cpus or quota < cpus) else cpus


def project(*, cap: int, running: int, used_rss: int, used_cpu: float, machine: Mapping[str, Any], cost: Cost) -> dict[str, Any]:
    """The machine with the cap filled: what the terminals would hold, and what that leaves.

    ``used_rss`` and ``used_cpu`` are the live terminals' sums now. The extra terminals are
    ``cap − running`` of the likely cost (none when the machine is already past the cap). Memory is
    judged as the whole machine would be — what everything else uses now plus the terminals at the
    cap — because the machine runs out of memory for everyone, not for terminals alone.
    """
    total, available = effective_memory(machine)
    extra = max(0, cap - running)
    extra_rss = extra * cost.rss_bytes
    terminals_rss = used_rss + extra_rss
    machine_used = max(0, total - available) + extra_rss if total else 0
    mem_percent = 100.0 * machine_used / total if total else 0.0
    cpus = effective_cpus(machine)
    extra_cpu = extra * cost.cpu_percent / cpus if cpus else 0.0
    cpu_percent = float(machine.get("cpu_percent") or 0.0) + extra_cpu
    return {
        "cap": cap,
        "sessions": max(cap, running),
        "terminals_rss_bytes": int(terminals_rss),
        "machine_used_bytes": int(machine_used),
        "mem_total_bytes": total,
        "mem_percent": round(mem_percent, 1),
        "cpu_percent": round(cpu_percent, 1),
        "level": level(max(mem_percent, 0.0)),
        "cpu_level": level(cpu_percent),
    }


def project_workloads(*, machine: Mapping[str, Any], kinds: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Several kinds of workload on one machine, each filled to its own cap: terminals and browsers.

    ``kinds`` maps a name to ``{cap, running, used_rss, cost}``. Each kind's extra is its own
    ``cap − running`` of its own likely cost; the machine is judged with all of them at once, since
    both fill the same memory — a projection per kind would call a machine with room for twenty
    terminals and two browsers "fine" twice while the two together did not fit.
    """
    total, available = effective_memory(machine)
    now = max(0, total - available) if total else 0
    out: dict[str, Any] = {}
    extra_all = 0.0
    extra_cpu = 0.0
    cpus = effective_cpus(machine)
    for name, kind in kinds.items():
        cost: Cost = kind["cost"]
        extra = max(0, int(kind["cap"]) - int(kind["running"]))
        extra_rss = extra * cost.rss_bytes
        extra_all += extra_rss
        extra_cpu += extra * cost.cpu_percent / cpus if cpus else 0.0
        out[name] = {"cap": int(kind["cap"]), "extra": extra, "rss_bytes": int(kind["used_rss"]), "at_cap_rss_bytes": int(kind["used_rss"] + extra_rss)}
    machine_used = now + extra_all if total else 0
    mem_percent = 100.0 * machine_used / total if total else 0.0
    cpu_percent = float(machine.get("cpu_percent") or 0.0) + extra_cpu
    return {
        "kinds": out,
        "machine_used_bytes": int(machine_used),
        "mem_total_bytes": total,
        "mem_percent": round(mem_percent, 1),
        "cpu_percent": round(cpu_percent, 1),
        "level": level(mem_percent),
        "cpu_level": level(cpu_percent),
    }


__all__ = ["DEFAULT_COST", "Cost", "ProfileCosts", "effective_cpus", "effective_memory", "level", "likely_cost", "project", "project_workloads"]
