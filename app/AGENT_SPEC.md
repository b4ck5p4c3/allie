# Allie — Agent Specification

## What Allie Is

Allie is a Python NFC reader service that acts as a **tag proxy**, not a self-contained lock. It reads NFC tags, publishes results to MQTT, and receives commands back over MQTT. A separate external service owns all access-control logic.

---

## System Components

| Component | File(s) | Responsibility |
|---|---|---|
| Main / Lifecycle | `src/main.py` | Reads config, boots all controllers |
| Config | `src/config.py` | Loads and validates `config.yaml` (Pydantic) |
| Accessory | `src/accessory.py` | HAP (HomeKit) server; translates HAP lock actions to MQTT events |
| MQTT | `src/service.py` | MQTT connection; routes inbound commands and outbound events |
| Reader | `src/reader/reader.py` | NFC polling loop; orchestrates tag-read pipeline |
| Homekey | `src/reader/homekey.py` | Homekey protocol handler |
| EMV | `src/reader/emv.py` | EMV transaction handler; extracts PAN |

---

## Tag-Read Pipeline

For every detected NFC tag, the reader runs this ordered pipeline and **stops at the first successful match**:

```
tag detected
  │
  ├─► is_homekey_capable?
  │     └─► homekey.handle(tag)  ──► publish "HK:<long-public-key>"
  │
  ├─► is_emv_capable?
  │     └─► emv.handle(tag)      ──► publish "EMV:<pan>"
  │
  └─► fallback                   ──► publish "UID:<iso-14443-uid>"
```

Tag details extracted before branching: **UID, SAK, ATQA**.

---

## MQTT Protocol

All topics are relative to a configurable prefix (default: `bus/devices/entrance-reader/`).

### Inbound Commands (Allie subscribes)

| Topic (relative) | Payload values | Effect |
|---|---|---|
| `indication/set` | `OFF` \| `IDLE` \| `READING` \| `DENIED` \| `SUCCESS_TAG` \| `SUCCESS_REMOTE` \| `RINGING` \| `GFU` | Drives the physical LED/buzzer animation |
| `lock/set` | `OPENED` \| `CLOSED` | Updates HAP lock state reported to HomeKit |

#### `indication/set` → P32/P71/P72 GPIO Mapping

Each `IndicationState` drives the physical LED/buzzer animation via the PN532's P32/P71/P72 GPIO port (written through `Reader._write_gpio`). The 3 bits give 8 combinations, one per state:

| State | P32 | P71 | P72 |
|---|---|---|---|
| `DENIED` | 0 | 0 | 0 |
| `IDLE` | 1 | 0 | 0 |
| `READING` | 0 | 1 | 0 |
| `ERROR` | 1 | 1 | 0 |
| `SUCCESS_TAG` | 0 | 0 | 1 |
| `SUCCESS_REMOTE` | 1 | 0 | 1 |
| `RINGING` | 0 | 1 | 1 |
| `OFF` | 1 | 1 | 1 |

### Outbound Events (Allie publishes)

| Topic (relative) | Payload format | Trigger |
|---|---|---|
| `events/tag` | `HK:<long-public-key>` | Homekey read |
| `events/tag` | `EMV:<pan>` | EMV read |
| `events/tag` | `UID:<iso-14443-uid>` | Plain UID read |
| `events/action` | `OPEN:<hap-device-id>` | HomeKit unlock request |
| `events/action` | `CLOSE:<hap-device-id>` | HomeKit lock request |

---

## Configuration (`config.yaml`)

```yaml
persistence: "./data"          # path for HAP/Homekey state files

nfc:
  path: "tty:usbserial-110:pn532"   # required — NFCpy device path
  broadcast: true                    # optional — ECP broadcast for Homekey

hap:
  bind_port: 51826             # optional
  bind_host: "0.0.0.0"        # optional

mqtt:
  host: "localhost"            # required
  port: 1883                   # optional
  username: "admin"            # optional
  password: "password"         # optional
  tls: false                   # optional
  ca_cert_path: "/path/to/ca.crt"  # optional
  prefix: "bus/devices/entrance-reader/"  # optional

homekey:
  express: true                # optional — Homekey express mode
  finish: "silver"             # optional — wallet card finish: tan | gold | silver | black
  flow: "fast"                 # optional — interaction flow: fast | standard | attestation
  serial_number: "BKSP.0010.03/0"  # optional — HAP device serial number
  pin_code: "031-45-154"       # optional — HAP pairing PIN
```

Configuration is validated at startup with **Pydantic**. Missing required fields cause an immediate fatal error.

---

## Reader Lifecycle & Error Handling

- Reader pre-configures the NFC hardware on startup.
- If the device disconnects or stops responding, the reader attempts recovery.
- If recovery fails, the reader **triggers an application restart** (the lifecycle controller handles this).

---

## External Service Contract

Allie makes **no access-control decisions**. The expected external-service flow is:

1. Allie publishes `events/tag` with the identifier.
2. External service checks whether the identifier is authorised.
3. If **authorised**: external service opens the physical door (via its own PLC) and sends an `indication/set SUCCESS_*` command to Allie.
4. If **denied**: external service sends `indication/set DENIED`.
5. On lock state changes: external service sends `lock/set OPENED|CLOSED`.
6. On HomeKit lock/unlock: Allie publishes `events/action OPEN|CLOSE:<hap-device-id>` and the external service acts accordingly.

---

## Non-Functional Requirements

- Python 3 — modern language features, strong typing (`typing`, Pydantic models).
- Linting: **flake8**; test runner: **tox**.
- Keep abstractions minimal — no deep class hierarchies. The five controllers listed above are the intended top-level units.
- All config fields typed and validated; no bare `dict` access.
