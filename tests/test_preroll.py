import pytest

from sidetap_live.preroll import PreRoll
from sidetap_live.types import BLOCK_BYTES

BLOCK = b"\x01" * BLOCK_BYTES


def test_it_keeps_only_the_most_recent_seconds():
    ring = PreRoll(seconds=1.0)          # 10 blocks at 100 ms
    for i in range(25):
        ring.add(bytes([i]) * BLOCK_BYTES)
    blocks = ring.drain()
    assert len(blocks) == 10
    assert blocks[0][0] == 15            # oldest surviving block
    assert blocks[-1][0] == 24


def test_seconds_reports_what_is_held():
    ring = PreRoll(seconds=3.0)
    for _ in range(5):
        ring.add(BLOCK)
    assert ring.seconds() == pytest.approx(0.5)


def test_drain_empties_it():
    ring = PreRoll(seconds=3.0)
    ring.add(BLOCK)
    assert ring.drain() == [BLOCK]
    assert ring.drain() == []
    assert ring.seconds() == 0.0
