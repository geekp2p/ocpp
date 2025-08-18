# cp_simulator.py
import asyncio
import logging
import json
from datetime import datetime

import websockets
from ocpp.routing import on
from ocpp.v16 import ChargePoint as CPBase
from ocpp.v16 import call, call_result
from ocpp.v16.enums import (
    Action,
    AuthorizationStatus,
    RegistrationStatus,
    RemoteStartStopStatus,
    DataTransferStatus,   # ⬅️ ใช้กับ DataTransfer.conf
)

logging.basicConfig(level=logging.INFO)

# ค่าคอนฟิกตัวอย่างฝั่ง CP (เอาไว้ตอบ GetConfiguration)
SUPPORTED_CONFIG = {
    # key: value (ตามสเปก value เป็น string)
    "HeartbeatInterval": "300",
    # ถ้าอยากให้ CSMS เห็นว่ารองรับ QRcodeConnectorID1 ให้ปลดคอมเมนต์บรรทัดล่าง
    # "QRcodeConnectorID1": "1",
}

class ChargePoint(CPBase):
    """Minimal OCPP 1.6 charge point simulator."""

    @on(Action.remote_start_transaction)
    async def on_remote_start(self, id_tag, connector_id, **kwargs):
        logging.info("RemoteStartTransaction received")
        async def _start_tx():
            req = call.StartTransaction(
                connector_id=connector_id,
                id_tag=id_tag,
                meter_start=0,
                timestamp=datetime.utcnow().isoformat() + "Z",
            )
            await self.call(req)
        asyncio.create_task(_start_tx())
        return call_result.RemoteStartTransaction(status=RemoteStartStopStatus.accepted)

    @on(Action.remote_stop_transaction)
    async def on_remote_stop(self, transaction_id, **kwargs):
        logging.info("RemoteStopTransaction received")
        async def _stop_tx():
            req = call.StopTransaction(
                transaction_id=transaction_id,
                meter_stop=0,
                timestamp=datetime.utcnow().isoformat() + "Z",
            )
            await self.call(req)
        asyncio.create_task(_stop_tx())
        return call_result.RemoteStopTransaction(status=RemoteStartStopStatus.accepted)

    @on(Action.authorize)
    async def on_authorize(self, id_tag, **kwargs):
        return call_result.Authorize(id_tag_info={"status": AuthorizationStatus.accepted})

    @on(Action.boot_notification)
    async def on_boot(self, charge_point_model, charge_point_vendor, **kwargs):
        return call_result.BootNotification(
            current_time=datetime.utcnow().isoformat() + "Z",
            interval=300,
            status=RegistrationStatus.accepted,
        )

    # ⬇️⬇️⬇️ เพิ่มสองแฮนด์เลอร์ที่ขาด เพื่อไม่ให้ NotImplementedError ⬇️⬇️⬇️
    @on(Action.get_configuration)
    async def on_get_configuration(self, key=None, **kwargs):
        """ตอบ GetConfiguration จาก CSMS
        - ถ้า CSMS ส่ง key=[] หรือไม่ส่งเลย: คืนค่าทั้งหมดที่รองรับ
        - ถ้าส่งมาเฉพาะบาง key: คืนที่มี และใส่ที่ไม่รู้จักลง unknown_key
        """
        requested = key or []  # list[str] หรือว่างแปลว่าขอทั้งหมด
        config_items = []
        unknown = []

        if not requested:
            # คืนทั้งหมดที่รองรับ
            for k, v in SUPPORTED_CONFIG.items():
                config_items.append({"key": k, "readonly": False, "value": v})
        else:
            for k in requested:
                if k in SUPPORTED_CONFIG:
                    config_items.append({"key": k, "readonly": False, "value": SUPPORTED_CONFIG[k]})
                else:
                    unknown.append(k)

        logging.info("→ GetConfiguration: return %d keys, unknown=%s", len(config_items), unknown)
        return call_result.GetConfiguration(
            configuration_key=config_items,
            unknown_key=unknown,
        )

    @on(Action.data_transfer)
    async def on_data_transfer(self, vendor_id, message_id=None, data=None, **kwargs):
        """รับ DataTransfer จาก CSMS (เช่นสั่งแสดง QR)
        แค่ตอบรับ (accepted) เพื่อไม่ให้ CSMS โยน NotImplementedError
        """
        logging.info("→ DataTransfer: vendor_id=%s message_id=%s data=%s", vendor_id, message_id, data)

        # ตัวอย่าง: ถ้าเป็นคำสั่งแสดง QR ก็ลอง parse JSON ดู (ไม่ทำอะไรมาก แค่ log)
        try:
            payload = json.loads(data) if isinstance(data, str) and data else {}
            msg_type = payload.get("message_type")
            uri = payload.get("uri")
            if vendor_id == "com.yourcompany.payment" and message_id == "DisplayQRCode" and msg_type == "QRCode":
                logging.info("   Display QRCode requested: %s", uri)
        except Exception as exc:
            logging.warning("   DataTransfer payload parse error: %s", exc)

        return call_result.DataTransfer(
            status=DataTransferStatus.accepted,
            data="ok"
        )
    # ⬆️⬆️⬆️ จบส่วนที่เพิ่ม ⬆️⬆️⬆️


async def main():
    url = "ws://45.136.236.186:9000/ocpp/CP_001"
    async with websockets.connect(url, subprotocols=["ocpp1.6"]) as ws:
        cp = ChargePoint("CP_001", ws)

        async def send_boot():
            req = call.BootNotification(
                charge_point_model="SimModel",
                charge_point_vendor="SimVendor",
            )
            await cp.call(req)

        await asyncio.gather(cp.start(), send_boot())


if __name__ == "__main__":
    asyncio.run(main())
