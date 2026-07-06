import pytest

import reader.reader as reader_module
from reader.reader import Reader
from service import IndicationState


class FakeDevice:
    """Just enough for Reader._write_gpio to be a no-op (device.chipset unused since we never assert on GPIO writes)."""


class FakeClf:
    def __init__(self):
        self.device = None  # Reader._write_gpio short-circuits when device is None


@pytest.fixture()
def reader():
    return Reader(clf=FakeClf(), repository=None, on_tag=lambda _: None)


@pytest.fixture()
def fake_clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(reader_module.time, "monotonic", lambda: now[0])
    return now


class TestIndicationQueue:
    def test_internal_transitions_apply_immediately(self, reader, fake_clock):
        reader.set_indication(IndicationState.IDLE)
        reader.set_indication(IndicationState.READING)

        assert reader._indication_current == IndicationState.READING
        assert reader._indication_next is None

    def test_external_indication_is_held_against_internal_override(self, reader, fake_clock):
        reader.set_indication(IndicationState.IDLE)
        reader.set_indication(IndicationState.DENIED, external=True)

        fake_clock[0] += 0.5
        reader.set_indication(IndicationState.IDLE)  # poll loop trying to reassert IDLE

        assert reader._indication_current == IndicationState.DENIED
        assert reader._indication_current_external is True
        assert reader._indication_next == IndicationState.IDLE

    def test_pending_next_is_promoted_once_hold_elapses(self, reader, fake_clock):
        reader.set_indication(IndicationState.DENIED, external=True)

        fake_clock[0] += 0.5
        reader.set_indication(IndicationState.IDLE)  # queued, hold not elapsed yet

        fake_clock[0] += 0.6  # total 1.1s since DENIED was set
        reader.set_indication(IndicationState.READING)

        assert reader._indication_current == IndicationState.READING
        assert reader._indication_current_external is False
        assert reader._indication_next is None

    def test_next_slot_is_filo_overridable(self, reader, fake_clock):
        reader.set_indication(IndicationState.DENIED, external=True)

        fake_clock[0] += 0.2
        reader.set_indication(IndicationState.IDLE)
        assert reader._indication_next == IndicationState.IDLE

        fake_clock[0] += 0.2
        reader.set_indication(IndicationState.SUCCESS_TAG, external=True)
        assert reader._indication_next == IndicationState.SUCCESS_TAG
        # current is still DENIED - hold not elapsed (0.4s < 1s)
        assert reader._indication_current == IndicationState.DENIED

    def test_external_indication_can_override_another_external_after_hold(self, reader, fake_clock):
        reader.set_indication(IndicationState.DENIED, external=True)

        fake_clock[0] += 1.0
        reader.set_indication(IndicationState.SUCCESS_TAG, external=True)

        assert reader._indication_current == IndicationState.SUCCESS_TAG
        assert reader._indication_current_external is True
