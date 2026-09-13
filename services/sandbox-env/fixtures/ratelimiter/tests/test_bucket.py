from ratelimiter import TokenBucket


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_starts_full_and_drains():
    clock = FakeClock()
    b = TokenBucket(capacity=3, refill_per_sec=1.0, clock=clock)
    assert b.try_acquire() and b.try_acquire() and b.try_acquire()
    assert not b.try_acquire()


def test_refills_over_time():
    clock = FakeClock()
    b = TokenBucket(capacity=2, refill_per_sec=1.0, clock=clock)
    assert b.try_acquire(2)
    clock.t += 1.5
    assert b.try_acquire(1)
    assert not b.try_acquire(1)


def test_never_exceeds_capacity():
    clock = FakeClock()
    b = TokenBucket(capacity=5, refill_per_sec=10.0, clock=clock)
    clock.t += 100
    assert b.tokens == 5
