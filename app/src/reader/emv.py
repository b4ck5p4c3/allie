"""
EMV (Europay, MasterCard, Visa) ICC reading capability.

This module is heavily inspired by emvcore.c from the proxmark3 project.
https://github.com/RfidResearchGroup/proxmark3
"""

import logging
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from util.iso7816 import ISO7816Command, ISO7816Tag
from util.tlv import BERTLV

log = logging.getLogger()


@dataclass
class EMVCard:
    """Represents extracted EMV card data."""

    pan: Optional[str] = None  # Primary Account Number
    expiry_month: Optional[int] = None
    expiry_year: Optional[int] = None
    cardholder_name: Optional[str] = None
    application_label: Optional[str] = None


class TLVDatabase:
    """
    Stores TLV data in a flat dictionary for easy lookup.

    Adapted from proxmark3's tlvdb structure.
    """

    def __init__(self):
        self.db: Dict[int, bytes] = {}

    def add_from_tlv_array(self, data: bytes):
        """Parse TLV data and add to database."""
        try:
            tlv_array = BERTLV.unpack_array(data)
            self._add_tlv_recursive(tlv_array)
        except Exception as e:
            log.warning(f"Error parsing TLV: {e}")

    def _add_tlv_recursive(self, tlv_array):
        """Recursively add TLV tags to database."""
        for tlv in tlv_array:
            tag_int = int.from_bytes(tlv.tag.data, "big")

            if tlv.tag.is_constructed and isinstance(tlv.value, list):
                packed_value = b"".join(child.pack() for child in tlv.value)
                self.db[tag_int] = packed_value
                self._add_tlv_recursive(tlv.value)
            else:
                self.db[tag_int] = tlv.value if isinstance(tlv.value, bytes) else bytes(tlv.value)

    def get(self, tag: int) -> Optional[bytes]:
        """Get value for tag from database"""
        return self.db.get(tag)

    def set(self, tag: int, value: bytes):
        """Set value for tag in database"""
        self.db[tag] = value


def dol_process(dol_data: Optional[bytes], tlv_db: TLVDatabase) -> bytes:
    """Process DOL (PDOL/CDOL/DDOL) by looking up tags in TLV database."""
    if not dol_data:
        return bytes()

    result = []
    i = 0

    while i < len(dol_data):
        # Parse tag (1 or 2 bytes)
        if dol_data[i] & 0x1F == 0x1F:
            if i + 1 >= len(dol_data):
                break
            tag = (dol_data[i] << 8) | dol_data[i + 1]
            i += 2
        else:
            tag = dol_data[i]
            i += 1

        if i >= len(dol_data):
            break
        length = dol_data[i]
        i += 1

        value = tlv_db.get(tag)
        if value:
            # Pad or truncate to required length
            if len(value) < length:
                value = value + bytes(length - len(value))
            elif len(value) > length:
                value = value[:length]
        else:
            value = bytes(length)

        result.extend(value)

    return bytes(result)


def param_load_defaults(tlv_db: TLVDatabase):
    """Load default transaction parameters into TLV database."""
    # Terminal Transaction Qualifiers (9F66)
    # Default: 0x26000000 (MSD and qVSDC supported, no CDA)
    tlv_db.set(0x9F66, bytes.fromhex("26000000"))

    # Terminal Country Code (9F1A) - US = 0840
    tlv_db.set(0x9F1A, bytes.fromhex("0840"))

    # Transaction Currency Code (5F2A) - USD = 0840
    tlv_db.set(0x5F2A, bytes.fromhex("0840"))

    # Transaction Date (9A) - Current date YYMMDD
    current_time = time.localtime()
    date_str = f"{current_time.tm_year % 100:02d}{current_time.tm_mon:02d}{current_time.tm_mday:02d}"
    tlv_db.set(0x9A, bytes.fromhex(date_str))

    # Transaction Type (9C) - 00 = goods and services
    tlv_db.set(0x9C, bytes.fromhex("00"))

    # Amount Authorized (9F02) - 6 bytes, 0.00
    tlv_db.set(0x9F02, bytes.fromhex("000000000000"))

    # Amount Other (9F03) - 6 bytes, 0.00
    tlv_db.set(0x9F03, bytes.fromhex("000000000000"))

    # Terminal Type (9F35) - 22 = merchant terminal attended online-only
    tlv_db.set(0x9F35, bytes.fromhex("22"))

    # Terminal Verification Results (95) - 5 bytes
    tlv_db.set(0x95, bytes.fromhex("0000000000"))

    # Transaction Time (9F21) - Current time HHMMSS
    time_str = (
        f"{current_time.tm_hour:02d}{current_time.tm_min:02d}{current_time.tm_sec:02d}"
    )
    tlv_db.set(0x9F21, bytes.fromhex(time_str))

    # Unpredictable Number (9F37) - 4 random bytes
    tlv_db.set(0x9F37, os.urandom(4))

    # Terminal Capabilities (9F33) - 3 bytes
    tlv_db.set(0x9F33, bytes.fromhex("E0F8C8"))

    # Additional Terminal Capabilities (9F40) - 5 bytes
    tlv_db.set(0x9F40, bytes.fromhex("6000F0A001"))


# Known AIDs for fallback when PPSE fails
KNOWN_AIDS = [
    "A0000000031010",  # Visa Debit/Credit
    "A0000000041010",  # MasterCard
    "A0000006581010",  # MIR
]


def emv_select_ppse(tag: ISO7816Tag) -> Tuple[bool, bytes, int]:
    """Select PPSE (Proximity Payment System Environment)."""
    ppse_name = b"2PAY.SYS.DDF01"

    command = ISO7816Command(
        cla=0x00, ins=0xA4, p1=0x04, p2=0x00, data=ppse_name, le=0x00
    )
    log.debug(f"SELECT PPSE CMD = {command}")

    response = tag.transceive(command)
    log.debug(f"SELECT PPSE RES = {response}")

    sw = (response.sw1 << 8) | response.sw2
    success = response.sw == (0x90, 0x00)

    return success, response.data, sw


def emv_search_ppse(tag: ISO7816Tag) -> Tuple[bool, Optional[List[bytes]]]:
    """
    Search for applications using PPSE.

    Returns:
        Tuple of (success, list_of_aids)
    """
    success, data, sw = emv_select_ppse(tag)

    if not success:
        log.debug(f"PPSE selection failed with SW: {sw:04x}")
        return False, None

    try:
        tlv_array = BERTLV.unpack_array(data)
        aids = extract_aids_from_tlv(tlv_array)
        return len(aids) > 0, aids
    except Exception as e:
        log.debug(f"Error extracting AIDs from PPSE: {e}")
        return False, None


def extract_aids_from_tlv(tlv_array) -> List[bytes]:
    """Extract all AIDs (tag 4F) from TLV array recursively"""
    aids = []

    for tlv in tlv_array:
        tag_int = int.from_bytes(tlv.tag.data, "big")

        if tag_int == 0x4F:  # AID tag
            aids.append(tlv.value if isinstance(tlv.value, bytes) else bytes(tlv.value))

        # Recurse into constructed TLVs
        if tlv.tag.is_constructed:
            try:
                # For constructed TLVs, value is already a list of child TLVs
                if isinstance(tlv.value, list):
                    aids.extend(extract_aids_from_tlv(tlv.value))
                else:
                    # If it's bytes, try to parse it
                    nested = BERTLV.unpack_array(tlv.value)
                    aids.extend(extract_aids_from_tlv(nested))
            except Exception:
                pass

    return aids


def emv_select(tag: ISO7816Tag, aid: bytes) -> Tuple[bool, bytes, int]:
    """Select application by AID."""
    command = ISO7816Command(cla=0x00, ins=0xA4, p1=0x04, p2=0x00, data=aid, le=0x00)
    log.debug(f"SELECT AID CMD = {command}")

    response = tag.transceive(command)
    log.debug(f"SELECT AID RES = {response}")

    sw = (response.sw1 << 8) | response.sw2
    success = response.sw == (0x90, 0x00)

    return success, response.data, sw


def emv_gpo(tag: ISO7816Tag, pdol_data: bytes) -> Tuple[bool, bytes, int]:
    """Execute Get Processing Options (GPO)."""
    # Wrap PDOL data in tag 0x83
    wrapped_data = bytes([0x83, len(pdol_data)]) + pdol_data

    command = ISO7816Command(
        cla=0x80, ins=0xA8, p1=0x00, p2=0x00, data=wrapped_data, le=0x00
    )
    log.debug(f"GPO CMD = {command}")

    response = tag.transceive(command)
    log.debug(f"GPO RES = {response}")

    sw = (response.sw1 << 8) | response.sw2
    success = response.sw == (0x90, 0x00)

    return success, response.data, sw


def emv_read_record(tag: ISO7816Tag, sfi: int, record: int) -> Tuple[bool, bytes, int]:
    """Read EMV record by SFI and record number."""
    p2 = (sfi << 3) | 0x04

    command = ISO7816Command(cla=0x00, ins=0xB2, p1=record, p2=p2, data=None, le=0x00)
    log.debug(f"READ RECORD CMD = {command}")

    response = tag.transceive(command)
    log.debug(f"READ RECORD RES = {response}")

    sw = (response.sw1 << 8) | response.sw2
    success = response.sw == (0x90, 0x00)

    return success, response.data, sw


def process_gpo_response_format1(gpo_data: bytes, tlv_db: TLVDatabase):
    """Process GPO response (Format 1: tag 0x80, Format 2: tag 0x77)."""
    try:
        tlv_array = BERTLV.unpack_array(gpo_data)

        for tlv in tlv_array:
            tag_int = int.from_bytes(tlv.tag.data, "big")

            if tag_int == 0x80:  # Format 1
                if len(tlv.value) < 2:
                    log.warning("GPO format 1 response too short")
                    return

                # AIP is first 2 bytes
                aip = tlv.value[0:2]
                tlv_db.set(0x82, aip)

                # AFL is remaining bytes
                if len(tlv.value) > 2:
                    afl = tlv.value[2:]
                    tlv_db.set(0x94, afl)

                log.debug(
                    f"GPO Format1: AIP={aip.hex()}, AFL={afl.hex() if len(tlv.value) > 2 else 'none'}"
                )
                return

            elif tag_int == 0x77:  # Format 2
                # Format 2 contains already-parsed TLV data
                if isinstance(tlv.value, list):
                    tlv_db._add_tlv_recursive(tlv.value)
                else:
                    tlv_db.add_from_tlv_array(tlv.value)
                log.debug("GPO Format2: TLV data added to database")
                return

    except Exception as e:
        log.debug(f"Error processing GPO response: {e}")


def get_pan_from_track2(track2_data: bytes) -> Optional[str]:
    """Extract PAN from Track 2 data (tag 0x57)."""
    if not track2_data or len(track2_data) < 8:
        return None

    track2_hex = track2_data.hex().upper()

    # Find separator 'D' or '='
    separator_pos = -1
    for sep in ["D", "="]:
        pos = track2_hex.find(sep)
        if pos != -1:
            separator_pos = pos
            break

    if separator_pos == -1:
        # No separator found, might be just PAN
        # Remove padding 'F'
        pan = track2_hex.rstrip("F")
    else:
        pan = track2_hex[:separator_pos]

    # Validate PAN length
    if len(pan) >= 13 and len(pan) <= 19:
        return pan

    return None


def _extract_pan_from_tlv_db(tlv_db: TLVDatabase) -> Optional[str]:
    """Try to extract PAN from TLV database (Track2 or direct PAN tag)."""
    # Try Track 2 data first (tag 0x57)
    track2 = tlv_db.get(0x57)
    if track2:
        pan = get_pan_from_track2(track2)
        if pan:
            return pan

    # Try direct PAN tag (0x5A)
    pan_data = tlv_db.get(0x5A)
    if pan_data:
        pan = pan_data.hex().rstrip("fF")
        if 13 <= len(pan) <= 19:
            return pan

    return None


def _extract_expiry_from_tlv_db(tlv_db: TLVDatabase) -> Tuple[Optional[int], Optional[int]]:
    """Extract expiry month and year from TLV database."""
    expiry = tlv_db.get(0x5F24)  # Application Expiration Date
    if expiry and len(expiry) >= 3:
        try:
            year = int(expiry[0:2].hex())
            month = int(expiry[2:3].hex())
            if month <= 12:
                return month, year
        except (ValueError, IndexError):
            pass
    return None, None


def read_emv_card(tag: ISO7816Tag) -> Optional[EMVCard]:
    """Read EMV card data (PAN, expiry, cardholder name)."""
    card = EMVCard()
    tlv_db = TLVDatabase()

    try:
        # Step 1: Try PPSE (contactless standard)
        log.debug("Attempting PPSE selection...")
        success, aids = emv_search_ppse(tag)

        if success and aids:
            log.debug(f"PPSE found {len(aids)} application(s)")
            aids_to_try = aids
        else:
            log.debug("PPSE failed, trying known AIDs...")
            aids_to_try = [bytes.fromhex(aid) for aid in KNOWN_AIDS]

        # Step 2: Try to select an application
        selected_aid = None
        for aid in aids_to_try:
            log.debug(f"Trying AID: {aid.hex()}")
            success, data, sw = emv_select(tag, aid)

            if success:
                log.debug(f"Successfully selected AID: {aid.hex()}")
                selected_aid = aid
                tlv_db.add_from_tlv_array(data)
                break
            else:
                log.debug(f"AID {aid.hex()} selection failed with SW: {sw:04x}")

        if not selected_aid:
            log.warning("No application could be selected")
            return None

        # Extract application label
        app_label = tlv_db.get(0x50)
        if app_label:
            try:
                card.application_label = app_label.decode("ascii", errors="ignore").strip()
            except Exception:
                pass

        # Step 3: Check if card already returned PAN in SELECT response
        card.pan = _extract_pan_from_tlv_db(tlv_db)
        if card.pan:
            log.debug(f"Found PAN in SELECT response: {card.pan}")
            card.expiry_month, card.expiry_year = _extract_expiry_from_tlv_db(tlv_db)
            return card

        # Step 4: Load defaults and execute GPO
        param_load_defaults(tlv_db)

        pdol = tlv_db.get(0x9F38) or tlv_db.get(0xDF71)  # Standard or MIR-specific
        pdol_data = dol_process(pdol, tlv_db)
        log.debug(f"PDOL data ({len(pdol_data)} bytes): {pdol_data.hex() if pdol_data else 'empty'}")

        success, gpo_data, sw = emv_gpo(tag, pdol_data)
        if not success:
            log.warning(f"GPO failed with SW: {sw:04x}")
            return None

        process_gpo_response_format1(gpo_data, tlv_db)
        tlv_db.add_from_tlv_array(gpo_data)

        # Try to extract PAN from GPO response
        card.pan = _extract_pan_from_tlv_db(tlv_db)
        if card.pan:
            log.debug(f"Found PAN in GPO response: {card.pan}")

        # Step 5: Read records from AFL if PAN not yet found
        afl = tlv_db.get(0x94)
        if afl and not card.pan:
            if len(afl) % 4 != 0:
                log.warning(f"Invalid AFL length: {len(afl)}")
            else:
                card.pan = _read_afl_records(tag, afl, tlv_db)

        # Extract remaining card data
        if not card.expiry_month or not card.expiry_year:
            card.expiry_month, card.expiry_year = _extract_expiry_from_tlv_db(tlv_db)

        if not card.cardholder_name:
            name = tlv_db.get(0x5F20)
            if name:
                try:
                    card.cardholder_name = name.decode("ascii", errors="ignore").strip()
                except Exception:
                    pass

        return card if card.pan else None

    except Exception as e:
        log.exception(f"Error during EMV read: {e}")
        return None


def _read_afl_records(tag: ISO7816Tag, afl: bytes, tlv_db: TLVDatabase) -> Optional[str]:
    """Read AFL records and return PAN if found."""
    log.debug(f"Reading AFL records: {afl.hex()}")

    for i in range(0, len(afl), 4):
        sfi = afl[i] >> 3
        first_record = afl[i + 1]
        last_record = afl[i + 2]

        log.debug(f"Reading SFI {sfi}, records {first_record} to {last_record}")

        for rec_num in range(first_record, last_record + 1):
            success, rec_data, sw = emv_read_record(tag, sfi, rec_num)

            if not success:
                log.debug(f"Failed to read SFI {sfi} record {rec_num}: SW={sw:04x}")
                continue

            tlv_db.add_from_tlv_array(rec_data)

            pan = _extract_pan_from_tlv_db(tlv_db)
            if pan:
                log.debug(f"Found PAN in record: {pan[:6]}******{pan[-4:]}")
                return pan

    return None
