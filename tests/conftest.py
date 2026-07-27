"""Check store invariants after every test, whether or not the test asserts on state.

This suite's demonstrated failure mode is that tests exercise the success path and
pass while the database ends up in a state no test looked at. Registering every
HubStore built during a test and checking it at teardown makes that state a
first-class assertion without each test having to remember one.
"""

from __future__ import annotations

import weakref

import pytest

from agent_hub.v2 import invariants
from agent_hub.v2.store import HubStore

_LIVE_STORES: weakref.WeakSet[HubStore] = weakref.WeakSet()


@pytest.fixture(autouse=True, scope="session")
def _track_stores():
    original = HubStore.__init__

    def tracked(self, *args, **kwargs):
        original(self, *args, **kwargs)
        _LIVE_STORES.add(self)

    HubStore.__init__ = tracked  # type: ignore[method-assign]
    try:
        yield
    finally:
        HubStore.__init__ = original  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def _assert_store_invariants(_track_stores):
    before = {store.path for store in _LIVE_STORES}
    try:
        yield
    finally:
        # Only inspect stores this test created, so one test's leftovers never
        # fail an unrelated one.
        for store in list(_LIVE_STORES):
            path = store.path
            if path in before or not path.exists():
                continue
            invariants.check_store(str(path))
