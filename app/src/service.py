"""MQTT connection; routes inbound commands and outbound events.

Allie makes no access-control decisions itself - it only publishes tag/action
events and applies indication/lock commands received from the external
access-control service, per AGENT_SPEC.md.
"""

import logging
from enum import Enum
from typing import Callable, Optional

import paho.mqtt.client as mqtt

from config import MqttConfig

log = logging.getLogger()


class IndicationState(str, Enum):
    OFF = "OFF"
    IDLE = "IDLE"
    READING = "READING"
    DENIED = "DENIED"
    SUCCESS_TAG = "SUCCESS_TAG"
    SUCCESS_REMOTE = "SUCCESS_REMOTE"
    RINGING = "RINGING"
    ERROR = "ERROR"


class LockState(str, Enum):
    OPENED = "OPENED"
    CLOSED = "CLOSED"


class MqttService:
    def __init__(self, config: MqttConfig) -> None:
        self.prefix = config.prefix

        # Overwritten by main.py to wire up the reader/accessory/indication hardware
        self.on_indication: Callable[[IndicationState], None] = lambda state: None
        self.on_lock_state: Callable[[LockState], None] = lambda state: None

        self._host = config.host
        self._port = config.port

        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if config.username is not None:
            self._client.username_pw_set(config.username, config.password)
        if config.tls:
            self._client.tls_set(ca_certs=config.ca_cert_path)

        self._client.on_connect = self._handle_connect
        self._client.on_message = self._handle_message

    def _topic(self, relative: str) -> str:
        return f"{self.prefix}{relative}"

    def start(self):
        self._client.connect(self._host, self._port)
        self._client.loop_start()

    def stop(self):
        self._client.loop_stop()
        self._client.disconnect()

    def _handle_connect(self, client, userdata, flags, reason_code, properties=None):
        log.info(f"Connected to MQTT broker {self._host}:{self._port} (reason={reason_code})")
        client.subscribe(self._topic("indication/set"))
        client.subscribe(self._topic("lock/set"))

    def _handle_message(self, client, userdata, msg):
        payload = msg.payload.decode().strip()

        if msg.topic == self._topic("indication/set"):
            self._dispatch(IndicationState, payload, self.on_indication)
        elif msg.topic == self._topic("lock/set"):
            self._dispatch(LockState, payload, self.on_lock_state)
        else:
            log.debug(f"Unhandled MQTT message on {msg.topic!r}: {payload!r}")

    @staticmethod
    def _dispatch(enum_type, payload: str, callback: Callable):
        try:
            callback(enum_type(payload))
        except ValueError:
            log.warning(f"Unknown {enum_type.__name__} value received: {payload!r}")

    def publish_tag_event(self, identifier: str):
        log.info(f"Publishing tag event: {identifier}")
        self._client.publish(self._topic("events/tag"), identifier)

    def publish_action_event(self, action: str, hap_device_id: Optional[str]):
        payload = f"{action}:{hap_device_id}"
        log.info(f"Publishing action event: {payload}")
        self._client.publish(self._topic("events/action"), payload)
