# Allie app version
__version__ = "0.1.0"

import logging
import os
import signal
import sys

from pyhap.accessory_driver import AccessoryDriver
from zeroconf import InterfaceChoice

from pathlib import Path
from accessory import Lock
from repository import Repository
from service import Service
from util.bfclf import BroadcastFrameContactlessFrontend

from config import config

# Resolve path relative to the project root
PERSISTENCE_PATH = Path(__file__).parent.joinpath("..", config.get("persistence", 'data')).resolve()


def configure_logging():
    log = logging.getLogger()
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)8s] %(module)-18s:%(lineno)-4d %(message)s"
    )
    hdlr = logging.StreamHandler(sys.stdout)

    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    log.setLevel(logging.getLevelNamesMapping().get(log_level, logging.INFO))
    hdlr.setFormatter(formatter)
    log.addHandler(hdlr)
    return log


def configure_nfc_device() -> BroadcastFrameContactlessFrontend:
    clf = BroadcastFrameContactlessFrontend(
        path=config.get("nfc.path"),
        broadcast_enabled=config.get("nfc.broadcast", True),
    )
    return clf


def configure_homekey_service(nfc_device: BroadcastFrameContactlessFrontend) -> Service:
    state_file_path = PERSISTENCE_PATH.joinpath("homekey.json")
    service = Service(
        nfc_device,
        repository=Repository(state_file_path),
        express=config.get("homekey.express", True),
        finish=config.get("homekey.finish", "silver"),
        flow=config.get("homekey.flow", "fast"),
    )
    return service


def configure_hap_accessory(homekey_service: Service) -> tuple[AccessoryDriver, Lock]:
    state_file_path = PERSISTENCE_PATH.joinpath("hap.json")
    driver = AccessoryDriver(
        port=config.get("hap.bind_port", 51826),
        address=config.get("hap.bind_host"),
        pincode=config.get("hap.pin_code", "031-45-154").encode(),
        interface_choice=InterfaceChoice.All,
        persist_file=str(state_file_path),
    )

    accessory = Lock(
        driver,
        "Allie",
        service=homekey_service,
        firmwareVersion=__version__,
    )

    driver.add_accessory(accessory=accessory)
    return driver, accessory


def main():
    log = configure_logging()
    nfc_device = configure_nfc_device()
    homekey_service = configure_homekey_service(nfc_device)
    hap_driver, _ = configure_hap_accessory(homekey_service)

    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(
            s,
            lambda *_: (
                log.info(f"SIGNAL {s}"),
                homekey_service.stop(),
                hap_driver.stop(),
            ),
        )

    homekey_service.start()
    hap_driver.start()


if __name__ == "__main__":
    main()
