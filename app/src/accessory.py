import base64
import functools
import logging
import os
import time

from pyhap.accessory import Accessory
from pyhap.const import CATEGORY_DOOR_LOCK

from config import config
from entity import (
    ControlPointRequest,
    ControlPointResponse,
    DeviceCredentialRequest,
    DeviceCredentialResponse,
    Endpoint,
    Enrollment,
    Enrollments,
    HardwareFinishColor,
    HardwareFinishResponse,
    Issuer,
    Operation,
    OperationStatus,
    ReaderKeyRequest,
    ReaderKeyResponse,
    SupportedConfigurationResponse,
)
from repository import Repository
from service import LockState, MqttService
from util.structable import pack_into_base64_string, unpack_from_base64_string

log = logging.getLogger()


# Lock class translates HAP lock actions to MQTT events and handles NFCAccess
# credential provisioning against the Repository. It makes no access-control
# decisions itself - opening the door is the external service's responsibility.
class Lock(Accessory):
    category = CATEGORY_DOOR_LOCK

    def __init__(
        self,
        *args,
        repository: Repository,
        mqtt_service: MqttService,
        firmwareVersion: str,
        finish: str = "silver",
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._last_client_public_keys = None

        self._lock_target_state = 1
        self._lock_current_state = 1

        self.repository = repository
        self.mqtt_service = mqtt_service

        try:
            self.hardware_finish_color = HardwareFinishColor[finish.upper()]
        except KeyError:
            self.hardware_finish_color = HardwareFinishColor.BLACK
            log.warning(
                f"HardwareFinish {finish} is not supported. Falling back to {self.hardware_finish_color}"
            )

        self.add_lock_service()
        self.add_nfc_access_service()
        self.add_unpair_hook()

        self.set_info_service(
            manufacturer='B4CKSP4CE',
            model='Allie Lock',
            firmware_revision=firmwareVersion,
            serial_number=config.hap.serial_number,
        )

    def on_tag_read(self, identifier: str):
        """Publish a tag identified by the Reader as an MQTT tag event."""
        self.mqtt_service.publish_tag_event(identifier)

    def apply_lock_state(self, state: LockState):
        """Apply a lock state reported by the external service via MQTT."""
        self._lock_current_state = self._lock_target_state = (
            0 if state == LockState.OPENED else 1
        )
        log.info(f"Applying lock state from MQTT: {state}")
        self.lock_current_state.set_value(self._lock_current_state, should_notify=True)
        self.lock_target_state.set_value(self._lock_target_state, should_notify=True)

    def add_unpair_hook(self):
        unpair = self.driver.unpair

        @functools.wraps(unpair)
        def patched_unpair(client_uuid):
            unpair(client_uuid)
            self.on_unpair(client_uuid)

        self.driver.unpair = patched_unpair

    def add_preload_service(self, service, chars=None, unique_id=None):
        """Create a service with the given name and add it to this acc."""
        if isinstance(service, str):
            service = self.driver.loader.get_service(service)
        if unique_id is not None:
            service.unique_id = unique_id
        if chars:
            chars = chars if isinstance(chars, list) else [chars]
            for char_name in chars:
                if isinstance(char_name, str):
                    char = self.driver.loader.get_char(char_name)
                    service.add_characteristic(char)
                else:
                    service.add_characteristic(char_name)
        self.add_service(service)
        return service

    def add_info_service(self):
        serv_info = self.driver.loader.get_service("AccessoryInformation")
        serv_info.configure_char("Name", value=self.display_name)
        serv_info.configure_char("SerialNumber", value="default")
        serv_info.add_characteristic(self.driver.loader.get_char("HardwareFinish"))
        serv_info.configure_char("HardwareFinish", getter_callback=self.get_hardware_finish)
        self.add_service(serv_info)

    def add_lock_service(self):
        self.service_lock_mechanism = self.add_preload_service("LockMechanism")

        self.lock_current_state = self.service_lock_mechanism.configure_char(
            "LockCurrentState", getter_callback=self.get_lock_current_state, value=0
        )

        self.lock_target_state = self.service_lock_mechanism.configure_char(
            "LockTargetState",
            getter_callback=self.get_lock_target_state,
            setter_callback=self.set_lock_target_state,
            value=0,
        )

        self.service_lock_management = self.add_preload_service("LockManagement")

        self.lock_control_point = self.service_lock_management.configure_char(
            "LockControlPoint",
            setter_callback=self.set_lock_control_point,
        )

        self.lock_version = self.service_lock_management.configure_char(
            "Version",
            getter_callback=self.get_lock_version,
        )

    def add_nfc_access_service(self):
        self.service_nfc = self.add_preload_service("NFCAccess")

        self.char_nfc_access_supported_configuration = self.service_nfc.configure_char(
            "NFCAccessSupportedConfiguration",
            getter_callback=self.get_nfc_access_supported_configuration,
        )

        self.char_nfc_access_control_point = self.service_nfc.configure_char(
            "NFCAccessControlPoint",
            getter_callback=self.get_nfc_access_control_point,
            setter_callback=self.set_nfc_access_control_point,
        )

        self.configuration_state = self.service_nfc.configure_char(
            "ConfigurationState", getter_callback=self.get_configuration_state
        )

    def _update_hap_pairings(self):
        client_public_keys = set(self.clients.values())
        if self._last_client_public_keys == client_public_keys:
            return
        self._last_client_public_keys = client_public_keys
        self.update_hap_pairings(client_public_keys)

    def get_lock_current_state(self):
        log.info("get_lock_current_state")
        return self._lock_current_state

    def get_lock_target_state(self):
        log.info("get_lock_target_state")
        return self._lock_target_state

    def set_lock_target_state(self, value):
        """Forward the HomeKit lock/unlock request as an MQTT action event.

        The current state is intentionally left unchanged here: it is only
        updated once the external service confirms the actual lock state via
        the `lock/set` MQTT command (see apply_lock_state).
        """
        log.info(f"set_lock_target_state {value}")
        self._lock_target_state = value
        action = "OPEN" if value == 0 else "CLOSE"
        self.mqtt_service.publish_action_event(action, config.hap.serial_number)
        return self._lock_target_state

    def get_lock_version(self):
        log.info("get_lock_version")
        return ""

    def set_lock_control_point(self, value):
        log.info(f"set_lock_control_point: {value}")

    def update_hap_pairings(self, issuer_public_keys):
        issuers = {
            issuer.public_key: issuer for issuer in self.repository.get_all_issuers()
        }
        for issuer in issuers.values():
            if issuer.public_key in issuer_public_keys:
                continue
            log.info(f"Removing issuer {issuer} as their pairing has been removed")
            self.repository.remove_issuer(issuer)

        for issuer_public_key in issuer_public_keys:
            if issuer_public_key in issuers:
                continue
            issuer = Issuer(public_key=issuer_public_key, endpoints=[])
            log.info(f"Adding issuer {issuer} based on paired clients")
            self.repository.upsert_issuer(issuer)

    def get_reader_key(self, request: ReaderKeyRequest) -> ReaderKeyResponse:
        return ReaderKeyResponse(
            key_identifier=self.repository.get_reader_group_identifier(),
        )

    def add_reader_key(self, request: ReaderKeyRequest) -> ReaderKeyResponse:
        changed = False
        if self.repository.get_reader_private_key() != request.reader_private_key:
            changed = True
            self.repository.set_reader_private_key(request.reader_private_key)
        if self.repository.get_reader_identifier() != request.unique_reader_identifier:
            changed = True
            self.repository.set_reader_identifier(request.unique_reader_identifier)
        return ReaderKeyResponse(
            status=OperationStatus.SUCCESS if changed else OperationStatus.DUPLICATE
        )

    def remove_reader_key(self, request: ReaderKeyRequest) -> ReaderKeyResponse:
        exists = request.key_identifier == self.repository.get_reader_group_identifier()
        if exists:
            self.repository.set_reader_private_key(bytes.fromhex("00" * 32))
        return ReaderKeyResponse(
            status=OperationStatus.SUCCESS if exists else OperationStatus.DOES_NOT_EXIST
        )

    def get_device_credential(
        self, request: DeviceCredentialRequest
    ) -> DeviceCredentialResponse:
        log.debug(f"get_device_credential request={request}")

    def add_device_credential(
        self, request: DeviceCredentialRequest
    ) -> DeviceCredentialResponse:
        endpoint = self.repository.get_endpoint_by_public_key(
            b"\x04" + request.credential_public_key
        )
        log.debug(f"add_device_credential endpoint={endpoint}")

        if endpoint is not None:
            if endpoint.enrollments.hap is not None:
                return DeviceCredentialResponse(
                    key_identifier=self.repository.get_reader_group_identifier(),
                    status=OperationStatus.DUPLICATE,
                )
            issuer = self.repository.get_issuer_by_id(request.issuer_key_identifier)
            endpoint.enrollments.hap = Enrollment(
                at=int(time.time()),
                payload=base64.b64encode(request.pack()).decode(),
            )
            self.repository.upsert_endpoint(issuer.id, endpoint)
            return DeviceCredentialResponse(
                key_identifier=self.repository.get_reader_group_identifier(),
                status=OperationStatus.SUCCESS,
            )

        issuer = self.repository.get_issuer_by_id(request.issuer_key_identifier)
        log.debug(f"add_device_credential issuer={issuer}")

        if issuer is None:
            return DeviceCredentialResponse(
                key_identifier=self.repository.get_reader_group_identifier(),
                status=OperationStatus.DOES_NOT_EXIST,
            )

        self.repository.upsert_endpoint(
            issuer.id,
            Endpoint(
                last_used_at=0,
                counter=0,
                key_type=request.key_type,
                public_key=b"\x04" + request.credential_public_key,
                persistent_key=os.urandom(32),
                enrollments=Enrollments(
                    hap=Enrollment(
                        at=int(time.time()),
                        payload=base64.b64encode(request.pack()).decode(),
                    ),
                    attestation=None,
                ),
            ),
        )
        return DeviceCredentialResponse(
            issuer_key_identifier=issuer.id, status=OperationStatus.SUCCESS
        )

    def remove_device_credential(
        self, request: DeviceCredentialRequest
    ) -> DeviceCredentialResponse:
        log.debug(f"remove_device_credential request={request}")

    # All methods down here are exposed as HAP characteristic callbacks
    def get_hardware_finish(self):
        self._update_hap_pairings()
        log.info("get_hardware_finish")
        return pack_into_base64_string(
            HardwareFinishResponse(color=self.hardware_finish_color)
        )

    def get_nfc_access_supported_configuration(self):
        self._update_hap_pairings()
        log.info("get_nfc_access_supported_configuration")
        return pack_into_base64_string(
            SupportedConfigurationResponse(
                number_of_issuer_keys=16, number_of_inactive_credentials=16
            )
        )

    def get_nfc_access_control_point(self):
        self._update_hap_pairings()
        log.info("get_nfc_access_control_point")
        return ""

    def set_nfc_access_control_point(self, value):
        self._update_hap_pairings()
        log.debug(f"<-- (B64) {value}")
        request_packed_tlv = unpack_from_base64_string(value)
        request: ControlPointRequest = ControlPointRequest.unpack(request_packed_tlv)
        log.debug(f"<-- (OBJ) {request}")
        response = ControlPointResponse()

        if request.device_credential_request is not None:
            response.device_credential_response = self._dispatch_operation(
                request.operation,
                get=self.get_device_credential,
                add=self.add_device_credential,
                remove=self.remove_device_credential,
                request=request.device_credential_request,
            )
        elif request.reader_key_request is not None:
            response.reader_key_response = self._dispatch_operation(
                request.operation,
                get=self.get_reader_key,
                add=self.add_reader_key,
                remove=self.remove_reader_key,
                request=request.reader_key_request,
            )

        log.debug(f"--> (OBJ) {response}")
        return pack_into_base64_string(response.pack())

    @staticmethod
    def _dispatch_operation(operation: Operation, *, get, add, remove, request):
        handlers = {
            Operation.GET: get,
            Operation.ADD: add,
            Operation.REMOVE: remove,
        }
        handler = handlers.get(operation)
        return handler(request) if handler is not None else None

    def get_configuration_state(self):
        self._update_hap_pairings()
        log.info("get_configuration_state")
        return 0

    @property
    def clients(self):
        return self.driver.state.paired_clients

    def on_unpair(self, client_id):
        log.info(f"on_unpair {client_id}")
        self._update_hap_pairings()
