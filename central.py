# central.py

import asyncio
import logging
import json
from datetime import datetime
from typing import List, Any, Dict
import itertools
import threading

from websockets import serve
from ocpp.routing import on
from ocpp.v16 import ChargePoint, call, call_result
from ocpp.v16.enums import RegistrationStatus, AuthorizationStatus, Action, RemoteStartStopStatus

# --- เพิ่ม import สำหรับ HTTP API ---
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
import uvicorn

logging.basicConfig(level=logging.INFO)

# === เก็บ reference ของ CP ที่ต่ออยู่ เพื่อเรียกใช้สั่ง start/stop ได้จากคอนโซล/HTTP ===
connected_cps: Dict[str, "CentralSystem"] = {}

# === ตัวนับ transactionId ที่ CSMS จะ “ออกเลข” ให้ StartTransaction.conf ===
_tx_counter = itertools.count(1)


def make_display_message_call(message_type: str, uri: str):
    """
    สร้าง fallback สำหรับแสดง QR:
    1) ถ้ามี call.DisplayMessage อยู่จริง พยายาม instantiate ด้วย signature ต่าง ๆ
    2) ถ้าไม่สำเร็จ fallback เป็น DataTransfer (ใช้ positional args เพราะบางเวอร์ชันไม่รับ keyword)
    """
    payload = {"message_type": message_type, "uri": uri}
    if hasattr(call, "DisplayMessage"):
        DisplayMessageCls = getattr(call, "DisplayMessage")
        for attempt_kwargs in ({"message": payload}, {"payload": payload}, {"content": payload}, {"display": payload}):
            try:
                instance = DisplayMessageCls(**attempt_kwargs)  # type: ignore
                logging.info(f"Instantiated call.DisplayMessage with args {attempt_kwargs}")
                return instance
            except Exception:
                continue
        logging.warning("call.DisplayMessage exists but all instantiation attempts failed; falling back to DataTransfer.")
    try:
        return call.DataTransferPayload("com.yourcompany.payment", "DisplayQRCode", json.dumps(payload))
    except Exception as e:
        logging.error(f"Failed to build DataTransfer fallback: {e}")
        raise


class CentralSystem(ChargePoint):
    """
    CSMS (Central) สำหรับ OCPP 1.6
    """

    # เก็บสถานะ transaction ต่อ connector เพื่อง่ายต่อการ stop
    # key: connector_id (int) -> value: transaction_id (int)
    def __init__(self, id, connection):
        super().__init__(id, connection)
        self.active_tx: Dict[int, int] = {}

    # เมธอดสั่งเริ่มชาร์จ
    async def remote_start(self, connector_id: int, id_tag: str):
        """
        ส่ง RemoteStartTransaction ไปยัง charger นี้
        """
        req = call.RemoteStartTransactionPayload(
            id_tag=id_tag,
            connector_id=connector_id
        )
        logging.info(f"→ RemoteStartTransaction to {self.id} (connector={connector_id}, idTag={id_tag})")
        resp = await self.call(req)
        logging.info(f"← RemoteStartTransaction.conf: {resp}")
        status = getattr(resp, "status", None)
        if status == RemoteStartStopStatus.accepted:
            logging.info("RemoteStartTransaction accepted (chargerจะส่ง StartTransaction.req ตามมา)")
        else:
            logging.warning(f"RemoteStartTransaction rejected: {status}")

    # เมธอดสั่งหยุดชาร์จ
    async def remote_stop(self, transaction_id: int):
        """
        ส่ง RemoteStopTransaction ด้วย transaction_id
        """
        req = call.RemoteStopTransactionPayload(transaction_id=transaction_id)
        logging.info(f"→ RemoteStopTransaction to {self.id} (tx={transaction_id})")
        resp = await self.call(req)
        logging.info(f"← RemoteStopTransaction.conf: {resp}")
        status = getattr(resp, "status", None)
        if status == RemoteStartStopStatus.accepted:
            logging.info("RemoteStopTransaction accepted")
        else:
            logging.warning(f"RemoteStopTransaction rejected: {status}")

    @on(Action.BootNotification)
    async def on_boot_notification(self, charge_point_model, charge_point_vendor, **kwargs):
        logging.info(f"← BootNotification from vendor={charge_point_vendor}, model={charge_point_model}")
        response = call_result.BootNotificationPayload(
            current_time=datetime.utcnow().isoformat() + "Z",
            interval=300,
            status=RegistrationStatus.accepted
        )

        # ดึง supported keys (optional)
        supported_keys: List[str] = []
        try:
            conf_req = call.GetConfigurationPayload()
            conf_resp = await asyncio.wait_for(self.call(conf_req), timeout=10)
            logging.info(f"→ GetConfiguration response: {conf_resp}")

            items: Any = []
            if hasattr(conf_resp, "configuration_key"):
                items = getattr(conf_resp, "configuration_key")
            elif hasattr(conf_resp, "configurationKey"):
                items = getattr(conf_resp, "configurationKey")
            elif isinstance(conf_resp, dict):
                items = conf_resp.get("configuration_key") or conf_resp.get("configurationKey") or []
            for entry in items:
                if isinstance(entry, dict):
                    key_name = entry.get("key")
                else:
                    key_name = getattr(entry, "key", None)
                if key_name:
                    supported_keys.append(key_name)
            logging.info(f"Supported configuration keys parsed: {supported_keys}")
        except asyncio.TimeoutError:
            logging.warning("Timeout fetching GetConfiguration; proceeding without supported keys.")
        except Exception as e:
            logging.warning(f"Failed to fetch supported configuration keys: {e}")

        # ตัวอย่างส่ง QR แสดงผล (optional)
        qr_url = "https://your-domain.com/qr?order_id=TEST123"
        target_key = "QRcodeConnectorID1"
        if target_key in supported_keys:
            logging.info(f"Using supported key '{target_key}' to send ChangeConfiguration for QR")
            change_req = call.ChangeConfigurationPayload(key=target_key, value=qr_url)
            asyncio.create_task(self._send_change_configuration(change_req))
        else:
            logging.info(f"Key '{target_key}' not supported; attempting fallback display (DisplayMessage/DataTransfer) for QR")
            try:
                fallback = make_display_message_call(message_type="QRCode", uri=qr_url)
                asyncio.create_task(self._send_change_configuration(fallback))
            except Exception as e:
                logging.error(f"Failed to send fallback display message: {e}")

        return response

    async def _send_change_configuration(self, request_payload):
        try:
            resp = await self.call(request_payload)
            logging.info(f"→ ChangeConfiguration / Custom response: {resp}")
        except Exception as e:
            logging.error(f"!!! ChangeConfiguration/custom failed: {e}")

    @on(Action.Authorize)
    async def on_authorize(self, id_tag, **kwargs):
        logging.info(f"← Authorize request, idTag={id_tag}")
        return call_result.AuthorizePayload(id_tag_info={"status": AuthorizationStatus.accepted})

    @on(Action.StatusNotification)
    async def on_status_notification(self, connector_id, error_code, status, **kwargs):
        logging.info(f"← StatusNotification: connector {connector_id} → status={status}, errorCode={error_code}")
        return call_result.StatusNotificationPayload()

    @on(Action.Heartbeat)
    def on_heartbeat(self, **kwargs):
        logging.info("← Heartbeat received")
        return call_result.HeartbeatPayload(current_time=datetime.utcnow().isoformat() + "Z")

    @on(Action.MeterValues)
    async def on_meter_values(self, connector_id, meter_value, **kwargs):
        logging.info(f"← MeterValues from connector {connector_id}: {meter_value}")
        return call_result.MeterValuesPayload()

    # ดักรับ StartTransaction เพื่อ “ออกเลข” และจดจำ transaction
    @on(Action.StartTransaction)
    async def on_start_transaction(self, connector_id, id_tag, meter_start, timestamp, reservation_id=None, **kwargs):
        tx_id = next(_tx_counter)  # CSMS ออกเลข transactionId
        self.active_tx[int(connector_id)] = tx_id
        logging.info(f"← StartTransaction from {self.id}: connector={connector_id}, idTag={id_tag}, meterStart={meter_start}")
        logging.info(f"→ Assign transactionId={tx_id}")
        return call_result.StartTransactionPayload(
            transaction_id=tx_id,
            id_tag_info={"status": AuthorizationStatus.accepted}
        )

    # ดักรับ StopTransaction เพื่อเคลียร์สถานะ
    @on(Action.StopTransaction)
    async def on_stop_transaction(self, transaction_id, meter_stop, timestamp, **kwargs):
        for c_id, t_id in list(self.active_tx.items()):
            if t_id == int(transaction_id):
                self.active_tx.pop(c_id, None)
                break
        logging.info(f"← StopTransaction from {self.id}: tx={transaction_id}, meterStop={meter_stop}")
        return call_result.StopTransactionPayload(
            id_tag_info={"status": AuthorizationStatus.accepted}
        )


# ================================
#        HTTP CONTROL API
# ================================
API_KEY = "changeme-123"  # เปลี่ยนเป็นค่า secret ของคุณ

app = FastAPI(title="OCPP Central Control API", version="1.0.0")

class StartReq(BaseModel):
    cpid: str
    connectorId: int
    idTag: str

class StopReq(BaseModel):
    cpid: str
    transactionId: int

def require_key(x_api_key: str | None):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid api key")

@app.post("/api/v1/start")
async def api_start(req: StartReq, x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    require_key(x_api_key)
    cp = connected_cps.get(req.cpid)
    if not cp:
        raise HTTPException(status_code=404, detail=f"ChargePoint '{req.cpid}' not connected")
    try:
        await cp.remote_start(req.connectorId, req.idTag)
        # ถ้า charger รับ จะตามด้วย StartTransaction.req → เราจะ assign transactionId ให้เอง
        return {"ok": True, "message": "RemoteStartTransaction sent"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/stop")
async def api_stop(req: StopReq, x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    require_key(x_api_key)
    cp = connected_cps.get(req.cpid)
    if not cp:
        raise HTTPException(status_code=404, detail=f"ChargePoint '{req.cpid}' not connected")
    try:
        await cp.remote_stop(req.transactionId)
        return {"ok": True, "message": "RemoteStopTransaction sent"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ================================
#    RUN OCPP WS + HTTP API
# ================================
async def run_http_api():
    """
    รัน FastAPI ด้วย uvicorn ภายใน event loop เดียวกัน
    """
    config = uvicorn.Config(app, host="0.0.0.0", port=8080, loop="asyncio", log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


async def main():
    """
    สร้าง WebSocket server รอฟังการเชื่อมต่อจาก Charger
    (Charger connect ด้วย ws://<host>:9000/ocpp/<ChargePointID>)
    พร้อมกันกับ HTTP API บน :8080
    """
    async def handler(websocket, path=None):
        if path is None:
            try:
                path = websocket.request.path
            except AttributeError:
                path = websocket.path if hasattr(websocket, "path") else ""
        cp_id = path.rsplit('/', 1)[-1] if path else "UNKNOWN"
        logging.info(f"[Central] New connection for Charge Point ID: {cp_id}")

        central = CentralSystem(cp_id, websocket)
        connected_cps[cp_id] = central
        try:
            await central.start()
        finally:
            connected_cps.pop(cp_id, None)
            logging.info(f"[Central] Disconnected: {cp_id}")

    # คอนโซลคำสั่งแบบง่าย ๆ ใน thread แยก (ใช้ควบคู่กับ REST ก็ได้)
    def console_thread(loop: asyncio.AbstractEventLoop):
        """
        คำสั่ง:
          start <cpid> <connector> <idTag>
          stop  <cpid> <txId>
          ls
          map <cpid>
        """
        while True:
            try:
                cmd = input().strip()
            except EOFError:
                return
            if not cmd:
                continue
            parts = cmd.split()
            if parts[0] == "ls":
                print("Connected CPs:", ", ".join(connected_cps.keys()) or "(none)")
                continue
            if parts[0] == "map" and len(parts) == 2:
                cp = connected_cps.get(parts[1])
                if not cp:
                    print("No such CP")
                else:
                    print(f"{parts[1]} active_tx:", cp.active_tx)
                continue
            if parts[0] == "start" and len(parts) >= 4:
                cpid, connector, idtag = parts[1], int(parts[2]), " ".join(parts[3:])
                cp = connected_cps.get(cpid)
                if not cp:
                    print("No such CP")
                    continue
                asyncio.run_coroutine_threadsafe(cp.remote_start(connector, idtag), loop)
                continue
            if parts[0] == "stop" and len(parts) == 3:
                cpid, txid = parts[1], int(parts[2])
                cp = connected_cps.get(cpid)
                if not cp:
                    print("No such CP")
                    continue
                asyncio.run_coroutine_threadsafe(cp.remote_stop(txid), loop)
                continue
            print("Unknown command. Examples: start CP_123 1 TESTTAG | stop CP_123 42 | ls | map CP_123")

    loop = asyncio.get_running_loop()
    threading.Thread(target=console_thread, args=(loop,), daemon=True).start()

    # สตาร์ท HTTP API ควบคู่กัน
    api_task = asyncio.create_task(run_http_api())

    async with serve(
        handler,
        host='0.0.0.0',
        port=9000,
        subprotocols=['ocpp1.6']
    ):
        logging.info("⚡ Central listening on ws://0.0.0.0:9000/ocpp/<ChargePointID> | HTTP :8080")
        await asyncio.Future()  # keep running

if __name__ == "__main__":
    asyncio.run(main())
