import asyncio
import logging
from datetime import datetime, timezone
from ocpp.routing import on
from ocpp.v16 import call_result, ChargePoint as CP
from ocpp.v16.enums import (
    AuthorizationStatus,
    RegistrationStatus,
    Action,
    RemoteStartStopStatus,
    DataTransferStatus,
)

class EVSEChargePoint(CP):
    def __init__(self, id, connection, model, send_status_cb, start_cb, stop_cb):
        super().__init__(id, connection)
        self.model = model
        self.send_status = send_status_cb
        self.on_start_local = start_cb
        self.on_stop_local = stop_cb

    # ====== CSMS -> EVSE ======

    @on(Action.RemoteStartTransaction)
    async def on_remote_start(self, id_tag, connector_id=None, **kwargs):
        cid = int(connector_id or 1)
        c = self.model.get(cid)
        # reject when not plugged or already charging
        if not c.plugged or c.session_active:
            return call_result.RemoteStartTransactionPayload(
                status=RemoteStartStopStatus.rejected
            )
        asyncio.create_task(self.on_start_local(cid, id_tag))
        return call_result.RemoteStartTransactionPayload(
            status=RemoteStartStopStatus.accepted
        )

    @on(Action.RemoteStopTransaction)
    async def on_remote_stop(self, transaction_id, **kwargs):
        # only stop if the transaction id belongs to an active session
        if self.model.get_by_tx(int(transaction_id)) is None:
            return call_result.RemoteStopTransactionPayload(
                status=RemoteStartStopStatus.rejected
            )
        asyncio.create_task(self.on_stop_local(int(transaction_id), None))
        return call_result.RemoteStopTransactionPayload(
            status=RemoteStartStopStatus.accepted
        )

    # ====== EVSE -> CSMS handlers ======
    @on(Action.BootNotification)
    async def on_boot(self, charge_point_model, charge_point_vendor, **kwargs):
        logging.info("BootNotification received")
        return call_result.BootNotificationPayload(
            current_time=datetime.now(timezone.utc).isoformat(),
            interval=300,
            status=RegistrationStatus.accepted
        )

    @on(Action.Heartbeat)
    async def on_heartbeat(self, **kwargs):
        return call_result.HeartbeatPayload(
            current_time=datetime.now(timezone.utc).isoformat()
        )

    @on(Action.Authorize)
    async def on_authorize(self, id_tag, **kwargs):
        return call_result.AuthorizePayload(id_tag_info={"status": AuthorizationStatus.accepted})

    @on(Action.StartTransaction)
    async def on_start_transaction(self, connector_id, id_tag, meter_start, timestamp, **kwargs):
        # CSMS ของคุณจะออก tx_id เองใน StartTransaction.conf
        # ที่นี่เราแค่รับ req แล้วตอบ accepted พร้อม meterStart
        await self.on_start_local(int(connector_id), id_tag)
        return call_result.StartTransactionPayload(
            transaction_id=0,  # จะถูกแทนด้วยเลขจาก CSMS ฝั่งคุณ
            id_tag_info={"status": AuthorizationStatus.accepted}
        )

    @on(Action.StopTransaction)
    async def on_stop_transaction(self, transaction_id, meter_stop, timestamp, **kwargs):
        await self.on_stop_local(int(transaction_id), meter_stop)
        return call_result.StopTransactionPayload(id_tag_info={"status": AuthorizationStatus.accepted})

    @on(Action.GetConfiguration)
    async def on_get_configuration(self, key: list | None = None, **kwargs):
        config = [
            {"key": "AuthorizeRemoteTxRequests", "readonly": False, "value": "false"},
            {"key": "AuthorizationCacheEnabled", "readonly": False, "value": "false"},
            {"key": "LocalAuthListEnabled", "readonly": False, "value": "true"},
            {"key": "LocalAuthListMaxLength", "readonly": True, "value": "100"},
            {"key": "ClockAlignedDataInterval", "readonly": False, "value": "1800"},
            {"key": "ConnectionTimeOut", "readonly": False, "value": "60"},
            {"key": "GetConfigurationMaxKeys", "readonly": True, "value": "100"},
            {"key": "HeartbeatInterval", "readonly": False, "value": "300"},
            {"key": "LocalAuthorizeOffline", "readonly": False, "value": "false"},
            {"key": "LocalPreAuthorize", "readonly": False, "value": "false"},
            {"key": "MeterValuesAlignedData", "readonly": False, "value": "Energy.Active.Import.Register,Current.Import,Voltage,Power.Active.Import,SoC,Temperature"},
            {"key": "MeterValuesAlignedDataMaxLength", "readonly": True, "value": "6"},
            {"key": "MeterValuesSampledData", "readonly": False, "value": "Energy.Active.Import.Register,Current.Import,Voltage,Power.Active.Import,SoC,Temperature,Power.Offered"},
            {"key": "MeterValuesSampledDataMaxLength", "readonly": True, "value": "7"},
            {"key": "MeterValueSampleInterval", "readonly": False, "value": "60"},
            {"key": "NumberOfConnectors", "readonly": True, "value": "2"},
            {"key": "ReserveConnectorZeroSupported", "readonly": True, "value": "false"},
            {"key": "ResetRetries", "readonly": False, "value": "120"},
            {"key": "ConnectorPhaseRotation", "readonly": False, "value": "NotApplicable"},
            {"key": "ConnectorPhaseRotationMaxLength", "readonly": True, "value": "1"},
            {"key": "StopTransactionOnEVSideDisconnect", "readonly": True, "value": "true"},
            {"key": "AllowOfflineTxForUnknownId", "readonly": False, "value": "false"},
            {"key": "StopTransactionOnInvalidId", "readonly": False, "value": "false"},
            {"key": "StopTxnAlignedData", "readonly": False, "value": "Energy.Active.Import.Register,Current.Import,Voltage,Power.Active.Import,SoC,Temperature"},
            {"key": "StopTxnAlignedDataMaxLength", "readonly": True, "value": "6"},
            {"key": "StopTxnSampledData", "readonly": False, "value": "Energy.Active.Import.Register,Current.Import,Voltage,Power.Active.Import,SoC,Temperature"},
            {"key": "StopTxnSampledDataMaxLength", "readonly": True, "value": "6"},
            {"key": "SupportedFeatureProfiles", "readonly": True, "value": "Core,FirmwareManagement,LocalAuthListManagement,Reservation,SmartCharging,RemoteTrigger"},
            {"key": "SupportedFeatureProfilesMaxLength", "readonly": True, "value": "6"},
            {"key": "TransactionMessageAttempts", "readonly": False, "value": "3"},
            {"key": "TransactionMessageRetryInterval", "readonly": False, "value": "60"},
            {"key": "UnlockConnectorOnEVSideDisconnect", "readonly": False, "value": "true"},
            {"key": "MaxEnergyOnInvalidId", "readonly": False, "value": "10"},
            {"key": "VendorInfo", "readonly": True, "value": "Gresgying"},
            {"key": "WebSocketPingInterval", "readonly": False, "value": "10"},
            {"key": "ChargeProfileMaxStackLevel", "readonly": True, "value": "20"},
            {"key": "ChargingScheduleAllowedChargingRateUnit", "readonly": True, "value": "Current,Power"},
            {"key": "ChargingScheduleMaxPeriods", "readonly": True, "value": "24"},
            {"key": "MaxChargingProfilesInstalled", "readonly": True, "value": "1"},
            {"key": "OcppUrl", "readonly": False, "value": "ws://45.136.236.186:9000/ocpp/Gresgying02"},
            {"key": "Rate", "readonly": False, "value": "0"},
            {"key": "Monetaryunit", "readonly": False, "value": "€"},
            {"key": "AutoCharge", "readonly": False, "value": "true"},
            {"key": "QRcodeConnectorID1", "readonly": False, "value": ""},
            {"key": "QRcodeConnectorID2", "readonly": False, "value": ""},
        ]
        if key:
            requested = set(key if isinstance(key, list) else [key])
            found = [item for item in config if item["key"] in requested]
            unknown = [k for k in requested if k not in {i["key"] for i in config}]
            return call_result.GetConfigurationPayload(
                configuration_key=found or None,
                unknown_key=unknown or None,
            )
        return call_result.GetConfigurationPayload(configuration_key=config)

    @on(Action.DataTransfer)
    async def on_data_transfer(self, vendor_id, **kwargs):
        return call_result.DataTransferPayload(status=DataTransferStatus.unknown_vendor_id)
