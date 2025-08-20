# central.py

import asyncio
import logging
import json
import hashlib
from datetime import datetime
from typing import List, Any, Dict, Tuple
import itertools
import threading

from websockets import serve
from ocpp.routing import on
from ocpp.v16 import ChargePoint, call, call_result
from ocpp.v16.enums import (
    RegistrationStatus,
    AuthorizationStatus,
    Action,
    RemoteStartStopStatus,
    DataTransferStatus,
)

# --- เพิ่ม import สำหรับ HTTP API ---
from fastapi import FastAPI, HTTPException, Header, Request
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

    # เก็บสถานะธุรกรรมต่อ connector เพื่อให้ทราบทั้ง transactionId และ idTag
    # key: connector_id (int) -> value: {"transaction_id": int, "id_tag": str}
    def __init__(self, id, connection):
        super().__init__(id, connection)
        self.active_tx: Dict[int, Dict[str, Any]] = {}
        # เก็บรายการ remote start ที่สั่งไว้ เพื่อใช้ตรวจสอบตอนรับ StartTransaction
        self.pending_remote: Dict[int, str] = {}
        # เก็บข้อมูลเพิ่มเติมระหว่างรอ StartTransaction (เช่น vid)
        self.pending_start: Dict[int, Dict[str, Any]] = {}
        # เก็บสถานะล่าสุดของแต่ละ connector
        self.connector_status: Dict[int, str] = {}
        # เก็บ task watchdog สำหรับ connector ที่ยังไม่มี session
        self.no_session_tasks: Dict[int, asyncio.Task] = {}

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
            # จดจำว่า connector นี้มี remote start pending
            self.pending_remote[int(connector_id)] = id_tag
            logging.info(
                "RemoteStartTransaction accepted (chargerจะส่ง StartTransaction.req ตามมา)"
            )
        else:
            logging.warning(f"RemoteStartTransaction rejected: {status}")
        return status

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

    async def unlock_connector(self, connector_id: int):
        """ส่งคำสั่ง UnlockConnector ไปยัง charger"""
        req = call.UnlockConnectorPayload(connector_id=connector_id)
        logging.info(f"→ UnlockConnector to {self.id} (connector={connector_id})")
        resp = await self.call(req)
        logging.info(f"← UnlockConnector.conf: {resp}")
        return getattr(resp, "status", None)

    async def _no_session_watchdog(self, connector_id: int, timeout: int = 90):
        """
        หากหัวรายงาน Preparing/Occupied แต่ยังไม่มีธุรกรรมภายใน timeout จะปลดล็อกสาย
        """
        try:
            await asyncio.sleep(timeout)
            status = self.connector_status.get(connector_id)
            if status in ("Preparing", "Occupied") and connector_id not in self.active_tx:
                logging.info(
                    f"No session started for connector {connector_id} after {timeout}s → unlocking"
                )
                await self.unlock_connector(connector_id)
                self.pending_remote.pop(connector_id, None)
                self.pending_start.pop(connector_id, None)
        except asyncio.CancelledError:
            logging.debug(f"Watchdog for connector {connector_id} cancelled")
        finally:
            self.no_session_tasks.pop(connector_id, None)

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
        logging.info(
            f"← StatusNotification: connector {connector_id} → status={status}, errorCode={error_code}"
        )
        c_id = int(connector_id)
        self.connector_status[c_id] = status
        # จับเวลาเมื่อหัวอยู่ในสถานะ Preparing/Occupied แต่ยังไม่มีธุรกรรม
        if status in ("Preparing", "Occupied"):
            if c_id not in self.active_tx and c_id not in self.no_session_tasks:
                self.no_session_tasks[c_id] = asyncio.create_task(
                    self._no_session_watchdog(c_id)
                )
        else:
            task = self.no_session_tasks.pop(c_id, None)
            if task:
                task.cancel()
        return call_result.StatusNotificationPayload()

    @on(Action.Heartbeat)
    def on_heartbeat(self, **kwargs):
        logging.info("← Heartbeat received")
        return call_result.HeartbeatPayload(current_time=datetime.utcnow().isoformat() + "Z")

    @on(Action.MeterValues)
    async def on_meter_values(self, connector_id, meter_value, **kwargs):
        logging.info(f"← MeterValues from connector {connector_id}: {meter_value}")
        return call_result.MeterValuesPayload()

    @on(Action.DataTransfer)
    async def on_data_transfer(self, vendor_id, message_id=None, data=None, **kwargs):
        """Handle custom DataTransfer messages from the charger."""
        logging.info(
            f"← DataTransfer: vendorId={vendor_id}, messageId={message_id}, data={data}"
        )
        return call_result.DataTransferPayload(status=DataTransferStatus.accepted)

    # ดักรับ StartTransaction เพื่อ “ออกเลข” และจดจำ transaction
    @on(Action.StartTransaction)
    async def on_start_transaction(self, connector_id, id_tag, meter_start, timestamp, reservation_id=None, **kwargs):
        expected = self.pending_remote.get(int(connector_id))
        if expected is not None and expected != id_tag:
            logging.warning(
                f"StartTransaction for connector {connector_id} received with unexpected idTag (expected={expected}, got={id_tag}); rejecting"
            )
            await self.unlock_connector(int(connector_id))
            self.pending_remote.pop(int(connector_id), None)
            self.pending_start.pop(int(connector_id), None)
            return call_result.StartTransactionPayload(
                transaction_id=0,
                id_tag_info={"status": AuthorizationStatus.invalid},
            )

        # ถ้ามี remote start pending ให้ลบ flag ทิ้ง
        pending = self.pending_start.pop(int(connector_id), None)
        self.pending_remote.pop(int(connector_id), None)

        # ไม่บังคับว่าต้องมี pending start เสมอ: รองรับ local start หรือ remote start ที่ไม่ได้ผ่าน API
        tx_id = next(_tx_counter)  # CSMS ออกเลข transactionId
        info = {
            "transaction_id": tx_id,
            "id_tag": id_tag,
        }
        if pending and "vid" in pending:
            info["vid"] = pending["vid"]
        # เก็บทั้ง transactionId และข้อมูลอื่นเพื่อให้ API ภายนอกเรียกดูได้
        self.active_tx[int(connector_id)] = info
        # ยกเลิก watchdog ถ้ามี
        task = self.no_session_tasks.pop(int(connector_id), None)
        if task:
            task.cancel()
        logging.info(
            f"← StartTransaction from {self.id}: connector={connector_id}, idTag={id_tag}, meterStart={meter_start}, vid={info.get('vid')}"
        )
        logging.info(f"→ Assign transactionId={tx_id}")
        return call_result.StartTransactionPayload(
            transaction_id=tx_id,
            id_tag_info={"status": AuthorizationStatus.accepted},
        )


# ดักรับ StopTransaction เพื่อเคลียร์สถานะ
    @on(Action.StopTransaction)
    async def on_stop_transaction(self, transaction_id, meter_stop, timestamp, **kwargs):
        for c_id, info in list(self.active_tx.items()):
            if info.get("transaction_id") == int(transaction_id):
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
DEFAULT_ID_TAG = "DEMO_IDTAG"

app = FastAPI(title="OCPP Central Control API", version="1.0.0")

def parse_kv(raw: str | None) -> Tuple[str, Dict[str, str]]:
    """Parse kv string into canonical sorted string and dict."""
    if not raw or raw.strip() == "-":
        return "-", {}
    kv_map: Dict[str, str] = {}
    for part in raw.split(","):
        if not part:
            continue
        key, _, value = part.partition("=")
        key = key.strip()
        value = value.strip()
        if not key or key == "hash":
            continue
        kv_map[key] = value
    if not kv_map:
        return "-", {}
    sorted_items = sorted(kv_map.items())
    sorted_str = ",".join(f"{k}={v}" for k, v in sorted_items)
    return sorted_str, kv_map

def compute_hash_canonical(
    cpid: str,
    connector_id: int,
    id_tag: str | None,
    tx_id: str | None,
    ts: str | None,
    vid: str | None,
    sorted_kv: str,
) -> str:
    """Compute SHA-256 hash of canonical string."""
    def norm(v: str | None) -> str:
        return v if v else "-"

    canonical = (
        f"{cpid}|{connector_id}|{norm(id_tag)}|{norm(tx_id)}|{norm(ts)}|{norm(vid)}|{norm(sorted_kv)}"
    )
    return hashlib.sha256(canonical.encode()).hexdigest()

@app.middleware("http")
async def log_requests(request: Request, call_next):
    logging.info(f">>> {request.method} {request.url.path}")
    try:
        response = await call_next(request)
        logging.info(f"<<< {request.method} {request.url.path} -> {response.status_code}")
        return response
    except Exception:
        logging.exception("Handler crashed")
        raise


@app.get("/api/v1/health")
def health():
    """Basic health check endpoint."""
    return {"ok": True, "time": datetime.utcnow().isoformat() + "Z"}

class StartReq(BaseModel):
    cpid: str
    connectorId: int
    idTag: str | None = None
    transactionId: int | None = None
    timestamp: str | None = None
    vid: str | None = None
    kv: str | None = None
    kvMap: Dict[str, str] | None = None
    hash: str | None = None

class StopReq(BaseModel):
    cpid: str
    transactionId: int | None = None
    connectorId: int | None = None
    idTag: str | None = None
    timestamp: str | None = None
    vid: str | None = None
    kv: str | None = None
    kvMap: Dict[str, str] | None = None
    hash: str | None = None

class StopByConnectorReq(BaseModel):
    cpid: str
    connectorId: int

class ReleaseReq(BaseModel):
    cpid: str
    connectorId: int

class ActiveSession(BaseModel):
    cpid: str
    connectorId: int
    idTag: str
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
        # compute hash if provided
        sorted_kv = "-"
        kv_map = {}
        if req.kvMap:
            kv_map = {k: v for k, v in req.kvMap.items() if k != "hash"}
            sorted_kv = ",".join(f"{k}={kv_map[k]}" for k in sorted(kv_map))
        elif req.kv:
            sorted_kv, kv_map = parse_kv(req.kv)
        expected_hash = compute_hash_canonical(
            req.cpid,
            req.connectorId,
            req.idTag,
            str(req.transactionId) if req.transactionId is not None else None,
            req.timestamp,
            req.vid,
            sorted_kv,
        )
        if req.hash and req.hash.lower() != expected_hash.lower():
            logging.warning(
                f"hash mismatch: provided={req.hash} computed={expected_hash}"
            )

        id_tag = req.idTag or DEFAULT_ID_TAG
        # เตรียมข้อมูล pending สำหรับ StartTransaction ที่จะตามมา
        cp.pending_start[int(req.connectorId)] = {"id_tag": id_tag}
        if req.vid:
            cp.pending_start[int(req.connectorId)]["vid"] = req.vid
        status = await cp.remote_start(req.connectorId, id_tag)
        if status != RemoteStartStopStatus.accepted:
            cp.pending_start.pop(int(req.connectorId), None)
        # ถ้า charger รับ จะตามด้วย StartTransaction.req → เราจะ assign transactionId ให้เอง
        return {"ok": True, "hash": expected_hash, "message": "RemoteStartTransaction sent"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/stop")
async def api_stop(req: StopReq, x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    require_key(x_api_key)
    cp = connected_cps.get(req.cpid)
    if not cp:
        raise HTTPException(status_code=404, detail=f"ChargePoint '{req.cpid}' not connected")
    try:
        sorted_kv = "-"
        kv_map = {}
        if req.kvMap:
            kv_map = {k: v for k, v in req.kvMap.items() if k != "hash"}
            sorted_kv = ",".join(f"{k}={kv_map[k]}" for k in sorted(kv_map))
        elif req.kv:
            sorted_kv, kv_map = parse_kv(req.kv)
        expected_hash = "-"
        if req.connectorId is not None:
            expected_hash = compute_hash_canonical(
                req.cpid,
                req.connectorId,
                req.idTag,
                str(req.transactionId) if req.transactionId is not None else None,
                req.timestamp,
                req.vid,
                sorted_kv,
            )
            if req.hash and req.hash.lower() != expected_hash.lower():
                logging.warning(
                    f"hash mismatch: provided={req.hash} computed={expected_hash}"
                )

        tx_id = req.transactionId
        if tx_id is None:
            session = None
            if req.connectorId is not None:
                session = cp.active_tx.get(req.connectorId)
                if session and req.idTag and session.get("id_tag") != req.idTag:
                    session = None
            if session is None and req.idTag:
                for info in cp.active_tx.values():
                    if info.get("id_tag") == req.idTag:
                        session = info
                        break
            if session:
                tx_id = session.get("transaction_id")
        if tx_id is None:
            if req.connectorId is not None:
                await cp.unlock_connector(req.connectorId)
            raise HTTPException(status_code=404, detail="No matching active transaction")
        await cp.remote_stop(tx_id)
        return {"ok": True, "transactionId": tx_id, "hash": expected_hash, "message": "RemoteStopTransaction sent"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/charge/stop")
async def api_stop_by_connector(req: StopByConnectorReq, x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    require_key(x_api_key)
    cp = connected_cps.get(req.cpid)
    if not cp:
        raise HTTPException(status_code=404, detail=f"ChargePoint '{req.cpid}' not connected")
    session = cp.active_tx.get(req.connectorId)
    if session is None:
        raise HTTPException(status_code=404, detail="No active transaction for this connector")
    tx_id = session["transaction_id"]
    try:
        await cp.remote_stop(tx_id)
        return {"ok": True, "transactionId": tx_id, "message": "RemoteStopTransaction sent"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/v1/release")
async def api_release(req: ReleaseReq, x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    """ปลดล็อกสายเมื่อยังไม่มีธุรกรรม"""
    require_key(x_api_key)
    cp = connected_cps.get(req.cpid)
    if not cp:
        raise HTTPException(status_code=404, detail=f"ChargePoint '{req.cpid}' not connected")
    if req.connectorId in cp.active_tx:
        raise HTTPException(status_code=400, detail="Connector has active transaction")
    task = cp.no_session_tasks.pop(req.connectorId, None)
    if task:
        task.cancel()
    cp.pending_remote.pop(req.connectorId, None)
    cp.pending_start.pop(req.connectorId, None)
    try:
        await cp.unlock_connector(req.connectorId)
        return {"ok": True, "message": "UnlockConnector sent"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/active")
async def api_active_sessions(x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    """คืนรายการธุรกรรมที่กำลังชาร์จอยู่ทั้งหมด."""
    require_key(x_api_key)
    sessions: list[ActiveSession] = []
    for cpid, cp in connected_cps.items():
        for conn_id, info in cp.active_tx.items():
            sessions.append(
                ActiveSession(
                    cpid=cpid,
                    connectorId=conn_id,
                    idTag=info.get("id_tag", ""),
                    transactionId=info.get("transaction_id", 0),
                )
            )
    return {"sessions": [s.dict() for s in sessions]}


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
          stop  <cpid> <connector|txId>
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
                cpid, num = parts[1], int(parts[2])
                cp = connected_cps.get(cpid)
                if not cp:
                    print("No such CP")
                    continue
                session = cp.active_tx.get(num)
                if session:
                    txid = session.get("transaction_id", num)
                    asyncio.run_coroutine_threadsafe(cp.remote_stop(txid), loop)
                    continue
                tx_match = None
                for info in cp.active_tx.values():
                    if info.get("transaction_id") == num:
                        tx_match = num
                        break
                if tx_match is not None:
                    asyncio.run_coroutine_threadsafe(cp.remote_stop(tx_match), loop)
                else:
                    asyncio.run_coroutine_threadsafe(cp.unlock_connector(num), loop)
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