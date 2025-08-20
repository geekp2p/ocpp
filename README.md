# ChargeForge Simulator

Simulates an OCPP 1.6J charge point that talks JSON over WebSocket. The
simulator has been exercised against the [Gresgying 120 kW–180 kW DC charging
station](https://www.gresgying.global/product/120kw-180kw-dc-charging-station.html)
and is intended for validating backend integrations. A reference CSMS
implementation is provided in [`HowToUse.me`](HowToUse.me), which shows how to
run the `central.py` server from the
[geekp2p/ocpp](https://github.com/geekp2p/ocpp) project when testing with real
hardware.

## ✅ Current features

- RemoteStart/RemoteStop with transactionId tracking per connector
- `/health` endpoint and Docker healthcheck
- Reconnect/backoff logic when the CSMS connection drops
- Basic state machine: Available → Preparing → Charging → Finishing → Available
- Periodic MeterValues with Wh increasing by a fixed rate
- HTTP control endpoints: `/plug/{cid}`, `/unplug/{cid}`, `/local_start/{cid}`, `/local_stop/{cid}`
- Uses the `ocpp` Python package with `subprotocols=['ocpp1.6']` for JSON over WebSocket

## 📋 Roadmap / Next Tasks

### 🔶 Core robustness
- [ ] **Multi-connector concurrency**: ให้ทุก connector เริ่ม/หยุดพร้อมกันได้จริง (ไม่แย่ง state กัน)
  - Acceptance: สั่ง remote start ที่ connector 1 และ 2 พร้อมกัน → ทั้งสองขึ้น Charging; stop เส้นใดเส้นหนึ่งไม่กระทบอีกเส้น
  - Implementation hints:
    - ตรวจโค้ด `send_meter_loop()` วนเฉพาะ `session_active=True` ต่อ connector (OK)
    - ยืนยันว่า `on_remote_stop()` และ `/local_stop/{cid}` เลือก `txId` ของ **cid นั้น** เท่านั้น
    - (ทางเลือก) ทำ **per-connector meter task** เพื่อแยกคาบได้อิสระ

- [ ] **Fault & Suspended states simulation**
  - Endpoints ที่ควรเพิ่ม:
    - `POST /fault/{cid}?code=GroundFailure` → ส่ง `StatusNotification(errorCode=GroundFailure, status=Faulted)`
    - `POST /suspend_ev/{cid}` / `POST /suspend_evse/{cid}` / `POST /resume/{cid}`
  - Acceptance: เรียก fault แล้ว CSMS เห็นสถานะ Faulted; resume กลับสู่ Charging/Available ได้

- [ ] **Metering fluctuations & extra measurands**
  - ENV เสนอ: `NOISE_W_PERCENT=5`, `EXTRA_MEASURANDS="Voltage,Current.Import,Power.Active.Import"`
  - ปรับ `send_meter_loop()` ให้เพิ่ม jitter (±NOISE%) และแนบ `Voltage/Current/Power` ใน `sampledValue`
  - Acceptance: ค่า Wh/Power/Voltage/Current ไม่คงที่ทุกคาบ; CSMS รับค่าถูกต้อง

### 🔒 Transport & Ops
- [ ] **WSS/TLS support**
  - ENV เสนอ:  
    `OCPP_WSS=true`, `SSL_VERIFY=true|false`, `CA_CERT=/certs/ca.pem`, `CLIENT_CERT=/certs/client.crt`, `CLIENT_KEY=/certs/client.key`
  - สร้าง `ssl.SSLContext` แล้วส่งให้ `websockets.connect(..., ssl=ctx)`
  - Acceptance: เชื่อม `wss://` กับ CSMS ที่เปิด TLS ได้; healthcheck ยัง green

- [ ] **/metrics (Prometheus) & /info**
  - `/metrics`: จำนวน sessions, energy ต่อ connector, error count
  - `/info`: dump คอนฟิก+สถานะคร่าว ๆ (cpid, connectors, active sessions)

### 🧪 Quality & Future
- [ ] **Integration tests (pytest)**
  - เทส flow: plug → local_start → มี MeterValues > 0 → local_stop → กลับ Available
  - (ถ้าสะดวก) รันคู่กับ CSMS จริงใน compose (service แยก) หรือ mock transport
- [ ] **OCPP 2.0.1 mode (optional/backlog)**  
  - ใส่ flag ใน `config.py` แต่ปักหมุด backlog ได้ หากยังใช้ 1.6J เป็นหลัก

### Requirements
- Python 3.10+ (ทดสอบกับ 3.12)
- ติดตั้ง dependencies ใน `sim/requirements.txt` (โดยใช้ `ocpp` 0.26.0 รองรับ OCPP 1.6J ผ่าน WebSocket)

### การใช้งาน how to use

# HowToUse

Instructions for running the reference `central.py` server from [geekp2p/ocpp](https://github.com/geekp2p/ocpp) and testing it with the Gresgying 120 kW–180 kW DC charging station or the ChargeForge simulator.

## 1. Setup `central.py`
1. Clone the project and save the provided `central.py`.
2. Install dependencies (Python 3.10+):
   ```bash
   pip install ocpp==0.26.0 websockets fastapi uvicorn
   ```
3. Start the CSMS:
   ```bash
   python central.py
   ```
   The server listens on `ws://0.0.0.0:9000/ocpp/<ChargePointID>` and exposes an HTTP API on `http://0.0.0.0:8080`.

## 2. Test with ChargeForge Simulator
1. Install simulator deps:
   ```bash
   pip install -r sim/requirements.txt
   ```
2. Start the simulator (connects to `ws://127.0.0.1:9000/ocpp` by default):
   ```bash
   python sim/evse.py
   ```
3. Use the CSMS HTTP API to control charging:
   ```bash
   curl -X POST -H 'X-API-Key: changeme-123' \
     -H 'Content-Type: application/json' \
     -d '{"cpid":"TestCP01","connectorId":1}' \
     http://localhost:8080/api/v1/start
   ```
   Use `/api/v1/stop` or `/api/v1/active` in a similar way. The simulator will report MeterValues and status updates.

## 3. Connecting a real Gresgying charger
1. Configure the charger to use WebSocket URL `ws://<csms-host>:9000/ocpp/<ChargePointID>` with OCPP 1.6J.
2. If the charger supports remote operations, invoke `/api/v1/start` and `/api/v1/stop` as above. Default API key: `changeme-123` (change it in `central.py`).
3. Monitor logs from `central.py` for BootNotification, StatusNotification, StartTransaction and StopTransaction events.

This setup has been validated with a Gresgying 120 kW–180 kW DC charging station using OCPP 1.6J over WebSocket.