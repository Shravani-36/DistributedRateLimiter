import time

import fakeredis
import pytest


@pytest.fixture
def redis():
    return fakeredis.FakeRedis(decode_responses=True)


class FakeClock:
    """A clock the test can move by hand.

    Tests about window edges and refill rates have to control where the edge
    falls. Sleeping towards a real one made them fail on a slow or loaded
    machine, where the requests themselves drifted across the boundary before
    the sleep did.
    """

    def __init__(self, now: float):
        self.now = now

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    """Freeze the clock the limiters actually read.

    They no longer take the time from Python - each Lua script asks Redis for
    it with redis.call('TIME'), which is what stops clock drift between API
    hosts from shifting the window. fakeredis answers TIME from time.time(),
    so patching that is what moves the window now.
    """
    fake = FakeClock(now=1_000_000.0)
    monkeypatch.setattr(time, "time", fake.time)
    return fake
