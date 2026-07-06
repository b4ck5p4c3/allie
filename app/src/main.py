# Allie app version
__version__ = "0.1.0"

import functools
import logging
import os
import signal
import sys

from pyhap.accessory_driver import AccessoryDriver
from zeroconf import InterfaceChoice

from pathlib import Path
from accessory import Lock
from reader.reader import Reader
from repository import Repository
from service import MqttService
from util.bfclf import BroadcastFrameContactlessFrontend

from config import config

# Resolve path relative to the project root
PERSISTENCE_PATH = Path(__file__).parent.joinpath("..", config.persistence).resolve()


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
        path=config.nfc.path,
        broadcast_enabled=config.nfc.broadcast,
    )
    return clf


def configure_reader(
    nfc_device: BroadcastFrameContactlessFrontend, repository: Repository
) -> Reader:
    return Reader(
        nfc_device,
        repository=repository,
        on_tag=lambda identifier: None,  # wired to the accessory in main()
        express=config.homekey.express,
        flow=config.homekey.flow,
    )


def configure_hap_accessory(
    repository: Repository, mqtt_service: MqttService
) -> tuple[AccessoryDriver, Lock]:
    state_file_path = PERSISTENCE_PATH.joinpath("hap.json")
    driver = AccessoryDriver(
        port=config.hap.bind_port,
        address=config.hap.bind_host,
        pincode=config.hap.pin_code.encode(),
        interface_choice=InterfaceChoice.All,
        persist_file=str(state_file_path),
    )

    accessory = Lock(
        driver,
        "Allie",
        repository=repository,
        mqtt_service=mqtt_service,
        firmwareVersion=__version__,
        finish=config.homekey.finish,
    )

    driver.add_accessory(accessory=accessory)
    return driver, accessory


def stop_hap_driver(hap_driver: AccessoryDriver, log: logging.Logger):
    """Stop the HAP accessory driver, guaranteeing its event loop halts.

    ``AccessoryDriver.async_stop()`` can raise (e.g. ``AttributeError`` on
    ``self.advertiser`` if the driver is signalled to stop before it has
    finished starting up). Since that coroutine is what eventually calls
    ``loop.stop()``, an unhandled exception there leaves the loop running
    forever and the process unkillable. Wrap it so the loop always stops.
    """

    async def _async_stop():
        try:
            await hap_driver.async_stop()
        except Exception:
            log.exception("Error while stopping HAP driver; forcing loop stop")
            hap_driver.loop.stop()

    hap_driver.loop.call_soon_threadsafe(
        hap_driver.loop.create_task, _async_stop()
    )


def handle_shutdown_signal(
    signum,
    frame,
    *,
    log: logging.Logger,
    reader: Reader,
    mqtt_service: MqttService,
    hap_driver: AccessoryDriver,
):
    log.info(f"SIGNAL {signum}")
    reader.stop()
    mqtt_service.stop()
    stop_hap_driver(hap_driver, log)


def main():
    log = configure_logging()

    nfc_device = configure_nfc_device()
    repository = Repository(PERSISTENCE_PATH.joinpath("homekey.json"))

    mqtt_service = MqttService(config.mqtt)
    reader = configure_reader(nfc_device, repository)
    hap_driver, lock = configure_hap_accessory(repository, mqtt_service)

    # Wire up: tag reads -> MQTT events/tag; MQTT lock/set -> HAP lock state;
    # MQTT indication/set -> reader GPIO (LED/buzzer indication)
    reader.on_tag = lock.on_tag_read
    mqtt_service.on_lock_state = lock.apply_lock_state
    mqtt_service.on_indication = lambda state: reader.set_indication(state, external=True)

    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(
            s,
            functools.partial(
                handle_shutdown_signal,
                log=log,
                reader=reader,
                mqtt_service=mqtt_service,
                hap_driver=hap_driver,
            ),
        )

    mqtt_service.start()
    reader.start()
    hap_driver.start()


if __name__ == "__main__":
    main()
