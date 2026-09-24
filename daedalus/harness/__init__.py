"""Command-line agents as staff: one adapter per CLI, and what they share.

The staff lifecycle — sessions, messages, requests, the launch queue — belongs to the orchestrator's
staff runtime. This package holds what is particular to running a CLI inside a terminal: the adapter
contract (``contract``), what each CLI offers (``capabilities``), how its events become a status
(``state``), and the environment it is launched with (``env``).

Adapters register themselves here as they are written, by the CLI's name, which is the ``harness``
column of a staff member.
"""

from __future__ import annotations

from daedalus.harness.capabilities import CAPABILITIES, Capabilities, capabilities
from daedalus.harness.contract import HarnessAdapter
from daedalus.harness.env import launch_environment, terminal_environment
from daedalus.harness.state import StaffState, StateContext, Transition, next_state

ADAPTERS: dict[str, type[HarnessAdapter]] = {}


class UnknownHarness(LookupError):
    """No adapter is registered for a harness: either the name is wrong, or its adapter is not built yet."""


def register(adapter: type[HarnessAdapter]) -> type[HarnessAdapter]:
    """Class decorator. Only a harness in the capability table can have an adapter, so a staff member
    never runs under a name the Harnesses screen and the orchestrator know nothing about."""
    name = adapter.name
    if name not in CAPABILITIES:
        raise UnknownHarness(f"{name!r} has no entry in the capability table")
    if name in ADAPTERS and ADAPTERS[name] is not adapter:
        raise ValueError(f"an adapter for {name!r} is already registered")
    ADAPTERS[name] = adapter
    return adapter


def adapter_for(name: str) -> type[HarnessAdapter]:
    try:
        return ADAPTERS[name]
    except KeyError:
        raise UnknownHarness(f"no adapter for {name!r}" if name in CAPABILITIES else f"no command-line harness is called {name!r}") from None


__all__ = [
    "ADAPTERS",
    "CAPABILITIES",
    "Capabilities",
    "HarnessAdapter",
    "StaffState",
    "StateContext",
    "Transition",
    "UnknownHarness",
    "adapter_for",
    "capabilities",
    "launch_environment",
    "next_state",
    "register",
    "terminal_environment",
]
