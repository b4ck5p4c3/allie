"""Typed, validated application configuration.

Configuration is loaded from ``config.yaml`` (with ``ALLIE_``-prefixed
environment variable overrides via Dynaconf) and validated into Pydantic
models. Missing required fields cause an immediate fatal error at startup.
"""

from typing import Optional

from dynaconf import Dynaconf
from pydantic import BaseModel


class NfcConfig(BaseModel):
    path: str
    broadcast: bool = True


class HapConfig(BaseModel):
    bind_port: int = 51826
    bind_host: Optional[str] = None
    pin_code: str = "031-45-154"
    serial_number: str = "BKSP.0010.03/0"


class MqttConfig(BaseModel):
    host: str
    port: int = 1883
    username: Optional[str] = None
    password: Optional[str] = None
    tls: bool = False
    ca_cert_path: Optional[str] = None
    prefix: str = "bus/devices/entrance-reader/"


class HomekeyConfig(BaseModel):
    express: bool = True
    finish: str = "silver"
    flow: str = "fast"


class AppConfig(BaseModel):
    persistence: str = "./data"
    nfc: NfcConfig
    hap: HapConfig = HapConfig()
    mqtt: MqttConfig
    homekey: HomekeyConfig = HomekeyConfig()


def _load_config() -> AppConfig:
    raw = Dynaconf(envvar_prefix="ALLIE", settings_files=["config.yaml"])
    return AppConfig(
        persistence=raw.get("persistence", "./data"),
        nfc=raw.get("nfc", {}),
        hap=raw.get("hap", {}),
        mqtt=raw.get("mqtt", {}),
        homekey=raw.get("homekey", {}),
    )


config = _load_config()
