"""NFC polling loop and tag-read pipeline.

For every detected NFC tag, runs the ordered pipeline described in
AGENT_SPEC.md and stops at the first successful match:

    tag detected
      |- is_homekey_capable? -> homekey.read_homekey(tag) -> "HK:<public-key>"
      |- is_emv_capable?     -> emv.read_emv_card(tag)     -> "EMV:<pan>"
      `- fallback            -> "UID:<iso-14443-uid>"

The reader makes no access-control decisions; it only identifies the tag
and reports the identifier via the ``on_tag`` callback.
"""

import logging
import threading
import time
from operator import attrgetter

from reader.emv import read_emv_card
from reader.homekey import ProtocolError, read_homekey
from repository import Repository
from service import IndicationState
from util.bfclf import (
    BroadcastFrameContactlessFrontend,
    ISODEPTag,
    RemoteTarget,
    activate,
)
from util.digital_key import DigitalKeyFlow, DigitalKeyTransactionType
from util.ecp import ECP
from util.iso7816 import ISO7816Tag
from util.threads import create_runner

log = logging.getLogger()

# indication/set -> P32/P71/P72 GPIO mapping, per AGENT_SPEC.md
_INDICATION_GPIO: dict[IndicationState, tuple[bool, bool, bool]] = {
    IndicationState.DENIED: (False, False, False),
    IndicationState.IDLE: (True, False, False),
    IndicationState.READING: (False, True, False),
    IndicationState.ERROR: (True, True, False),
    IndicationState.SUCCESS_TAG: (False, False, True),
    IndicationState.SUCCESS_REMOTE: (True, False, True),
    IndicationState.RINGING: (False, True, True),
    IndicationState.OFF: (True, True, True),
}


class Reader:
    """Polls for NFC tags and identifies them via the Homekey/EMV/UID pipeline."""

    # Minimum time (seconds) an externally-set (MQTT) indication stays on the
    # GPIO port before it can be overridden by another indication.
    INDICATION_MIN_HOLD = 1.0

    def __init__(
        self,
        clf: BroadcastFrameContactlessFrontend,
        repository: Repository,
        on_tag: callable,
        express: bool = True,
        flow: str = "fast",
        throttle_polling: float = 0.15,
    ) -> None:
        self.clf = clf
        self.repository = repository
        self.on_tag = on_tag
        self.throttle_polling = throttle_polling
        self.express = express in (True, "True", "true", "1")

        try:
            self.flow = DigitalKeyFlow[flow.upper()]
        except KeyError:
            self.flow = DigitalKeyFlow.FAST
            log.warning(
                f"Digital Key flow {flow} is not supported. Falling back to {self.flow}"
            )

        self._run_flag = True
        self._runner = None

        # Two-slot indication queue: "current" is whatever is currently applied
        # to the GPIO port; "next" is a single overridable (FILO) pending slot
        # used to hold a request that arrived while an externally-set (MQTT)
        # indication hasn't finished its minimum on-screen time yet.
        self._indication_lock = threading.Lock()
        self._indication_current: IndicationState | None = None
        self._indication_current_since: float = 0.0
        self._indication_current_external: bool = False
        self._indication_next: IndicationState | None = None
        self._indication_next_external: bool = False

    def _write_gpio(self, p32: bool, p71: bool, p72: bool):
        """Write P32, P71, P72 GPIO pins on the PN532 (used for RF field control)."""
        if self.clf.device is None:
            return
        try:
            p7 = (int(p71) << 1) | (int(p72) << 2)
            # 0x80 is the validation bit for P7 port
            params = bytearray([0x80 | (int(p32 << 2)), 0x80 | p7])
            self.clf.device.chipset.command(0x0E, params, timeout=0.1)
        except Exception as e:
            log.debug(f"write_gpio failed: {e}")

    def set_indication(self, state: IndicationState, external: bool = False) -> None:
        """Apply an indication to the P32/P71/P72 port.

        The poll loop drives IDLE/READING internally (``external=False``) on
        every cycle for RF sensing, which used to immediately stomp over any
        indication set via MQTT (``external=True``). To fix that, indications
        are arbitrated through a two-slot queue:

        - "current": the indication presently on the GPIO port.
        - "next": a single overridable (FILO) pending slot for a request that
          arrives while "current" is a not-yet-expired externally-set
          indication - only the most recent such request is kept (older ones
          are discarded), and it becomes "current" once the hold expires.

        This guarantees any MQTT-set indication stays visible for at least
        ``INDICATION_MIN_HOLD`` seconds before Allie's own polling logic (or a
        newer MQTT command) can replace it.
        """
        now = time.monotonic()
        with self._indication_lock:
            # Promote a pending "next" once the current indication's minimum
            # hold has elapsed (or if current was never externally-set).
            if self._indication_next is not None and not self._is_holding(now):
                self._apply_indication(
                    self._indication_next, self._indication_next_external, now
                )

            if self._is_holding(now):
                # Current indication hasn't finished its minimum hold yet:
                # queue this request, overwriting any previously queued one.
                self._indication_next = state
                self._indication_next_external = external
                return

            self._apply_indication(state, external, now)

    def _is_holding(self, now: float) -> bool:
        """Whether the current indication is externally-set and still within its minimum hold."""
        return (
            self._indication_current_external
            and self._indication_current is not None
            and (now - self._indication_current_since) < self.INDICATION_MIN_HOLD
        )

    def _apply_indication(self, state: IndicationState, external: bool, now: float) -> None:
        self._indication_current = state
        self._indication_current_since = now
        self._indication_current_external = external
        self._indication_next = None
        self._indication_next_external = False

        p32, p71, p72 = _INDICATION_GPIO[state]
        self._write_gpio(p32, p71, p72)

    def start(self):
        self._runner = create_runner(
            name="reader",
            target=self.run,
            flag=attrgetter("_run_flag"),
            delay=0,
            exception_delay=1,
            start=True,
        )

    def stop(self):
        self._run_flag = False
        if self._runner is not None:
            self._runner.join()

    def run(self):
        if self.repository.get_reader_private_key() in (None, b""):
            raise Exception("Device is not configured via HAP. NFC inactive")

        log.debug("Connecting to the NFC reader...")

        self.clf.device = None
        self.clf.open(self.clf.path)
        if self.clf.device is None:
            raise Exception(
                f"Could not connect to NFC device {self.clf} at {self.clf.path}"
            )

        while self._run_flag:
            self._poll()

    def _poll(self):
        start = time.monotonic()

        # Set Idle
        # self.set_indication(IndicationState.IDLE)

        remote_target = self.clf.sense(
            RemoteTarget("106A"),
            broadcast=ECP.home(
                identifier=self.repository.get_reader_group_identifier(),
                flag_2=self.express,
            ).pack(),
        )

        if remote_target is None:
            # Throttle polling attempts to prevent overheating & RF performance degradation
            time.sleep(max(0, self.throttle_polling - time.monotonic() + start))
            return

        # Set Reading
        self.set_indication(IndicationState.READING)

        try:
            target = activate(self.clf, remote_target)
            if target is None:
                return

            if not isinstance(target, ISODEPTag):
                self.on_tag("UID:" + target.identifier.hex())
                self._wait_until_target_leaves_field()
                return

            log.debug(f"Got NFC tag {target}")

            tag = ISO7816Tag(target)
            if not self._try_homekey(tag, start):
                self._try_emv(tag, start)

            # Let device cool down, wait for ISODEP to drop to consider comms finished
            while target.is_present:
                log.debug("Waiting for device to leave the field...")
                time.sleep(0.5)

            log.debug("Device left the field. Continuing in 2 seconds...")
            time.sleep(2)
            log.debug("Waiting for next device...")
        except Exception as e:
            self.set_indication(IndicationState.ERROR)
            raise e

        # Get back to IDLE indication
        # self.set_indication(IndicationState.IDLE)

    def _wait_until_target_leaves_field(self):
        while self.clf.sense(RemoteTarget("106A")) is not None:
            log.debug("Waiting for target to leave the field...")
            time.sleep(0.5)

    def _try_homekey(self, tag: ISO7816Tag, start: float) -> bool:
        """Attempt Homekey authentication. Returns True if the tag was Homekey-capable."""
        try:
            result_flow, new_issuers_state, endpoint = read_homekey(
                tag,
                issuers=self.repository.get_all_issuers(),
                preferred_versions=[b"\x02\x00"],
                flow=self.flow,
                transaction_code=DigitalKeyTransactionType.UNLOCK,
                reader_identifier=self.repository.get_reader_group_identifier()
                + self.repository.get_reader_identifier(),
                reader_private_key=self.repository.get_reader_private_key(),
                key_size=16,
            )
        except ProtocolError as e:
            log.debug(f'Could not authenticate device due to protocol error "{e}"')
            return False

        if new_issuers_state is not None and len(new_issuers_state):
            self.repository.upsert_issuers(new_issuers_state)

        log.debug(f"Authenticated endpoint via {result_flow!r}: {endpoint}")
        log.debug(f"Transaction took {(time.monotonic() - start) * 1000} ms")

        if endpoint is not None:
            self.on_tag("HK:" + endpoint.public_key.hex())

        return True

    def _try_emv(self, tag: ISO7816Tag, start: float):
        """Try to read PAN from an EMV card when the Home Key applet is not found."""
        try:
            log.debug("Attempting to read as EMV card...")
            card = read_emv_card(tag)
            if card and card.pan:
                masked_pan = card.pan[:6] + "*" * (len(card.pan) - 10) + card.pan[-4:]
                log.debug(
                    f"EMV Card detected - PAN: {masked_pan}"
                    + (f", Expiry: {card.expiry_month:02d}/{card.expiry_year:02d}"
                       if card.expiry_month and card.expiry_year else "")
                    + (f", Name: {card.cardholder_name}" if card.cardholder_name else "")
                    + (f", Label: {card.application_label}" if card.application_label else "")
                )
                log.debug(f"EMV transaction took {(time.monotonic() - start) * 1000:.1f} ms")

                self.on_tag("EMV:" + card.pan)
            else:
                log.debug("No EMV card data found")
        except Exception as e:
            log.warning(f'Unexpected error during EMV read: "{e}"')
