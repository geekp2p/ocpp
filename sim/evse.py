import asyncio
import json
import logging
from datetime import datetime, timezone
import random
import ssl

import uvicorn
from fastapi import FastAPI
import websockets

from ocpp.v16 import call
from ocpp.v16.enums import Action, Measurand
# from ocpp.transport import WebSocketTransport

from .config import *
from .state_machine import EVSEModel, EVSEState
from .ocpp_handlers import EVSEChargePoint

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

app = FastAPI(title="ChargeForge-Sim Control")


@app.get("/health")
async def health():
    return {"ok": True}

model = EVSEModel(connectors=CONNECTORS, meter_start_wh=METER_START_WH)
cp = None  # type: ignore

# -------- helper: send StatusNotification --------
async def send_status(connector_id: int):
    global cp
    c = model.get(connector_id)
    st = c.to_status()
    req = call.StatusNotificationPayload(
        connector_id=connector_id,
        error_code=c.error_code,
        status=st,
        timestamp=datetime.now(timezone.utc).isoformat()
    )
    await cp.call(req)  # type: ignore
    logging.info(
        f"StatusNotification sent: connector={connector_id}, status={st}, error={c.error_code}"
    )

# -------- local state transitions --------
async def start_local(connector_id: int, id_tag: str):
    c = model.get(connector_id)
    c.id_tag = id_tag
    c.session_active = True
    c.state = EVSEState.CHARGING
    await send_status(connector_id)
    # inform CSMS and store transaction id
    req = call.StartTransactionPayload(
        connector_id=connector_id,
        id_tag=id_tag,
        meter_start=c.meter_wh,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
    conf = await cp.call(req)  # type: ignore
    model.assign_tx(connector_id, conf.transaction_id)
    logging.info(
        f"StartTransaction confirmed: connector={connector_id}, tx_id={conf.transaction_id}"
    )

async def stop_local_by_tx(tx_id: int, meter_stop: int | None = None):
    c = model.get_by_tx(tx_id)
    if c is None:
        return
    if meter_stop is None:
        meter_stop = c.meter_wh
    req = call.StopTransactionPayload(
        transaction_id=tx_id,
        meter_stop=meter_stop,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
    await cp.call(req)  # type: ignore
    c.state = EVSEState.FINISHING
    await send_status(c.id)
    await asyncio.sleep(1)
    c.state = EVSEState.AVAILABLE
    c.id_tag = None
    await send_status(c.id)
    model.clear_tx(tx_id)
    return

# -------- OCPP client main --------
async def ocpp_client():
    global cp
    cpid = CPID
    url = f"{CSMS_URL}/{cpid}"
    ssl_context = None
    if CSMS_URL.startswith("wss://"):
        ssl_context = ssl.create_default_context(cafile=TLS_CA_CERT) if TLS_CA_CERT else ssl.create_default_context()
        if TLS_CLIENT_CERT and TLS_CLIENT_KEY:
            ssl_context.load_cert_chain(TLS_CLIENT_CERT, TLS_CLIENT_KEY)
    while True:
        try:
            logging.info(f"Connecting to CSMS: {url}")
            async with websockets.connect(url, subprotocols=['ocpp1.6'], ssl=ssl_context) as ws:
                cp = EVSEChargePoint(
                    cpid, ws, model,
                    send_status_cb=send_status,
                    start_cb=start_local,
                    stop_cb=stop_local_by_tx
                )
            # async with websockets.connect(url, subprotocols=['ocpp1.6'], ssl=ssl_context) as ws:
            #     transport = WebSocketTransport(ws)
            #     cp = EVSEChargePoint(
            #         cpid, transport, model,
            #         send_status_cb=send_status,
            #         start_cb=start_local,
            #         stop_cb=stop_local_by_tx
            #     )
                # Boot → Available
                asyncio.create_task(cp.start())
                await asyncio.sleep(1)
                # boot_req = call.BootNotificationPayload(
                #     charge_point_model="CF-Sim",
                #     charge_point_vendor="ChargeForge",
                # )
                # await cp.call(boot_req)
                # for cid in model.connectors.keys():
                #     await send_status(cid)
                boot_req = call.BootNotificationPayload(
                    charge_point_model=CP_MODEL,
                    charge_point_vendor=CP_VENDOR,
                    charge_point_serial_number=CP_SERIAL_NUMBER,
                    firmware_version=FIRMWARE_VERSION,
                    iccid=ICCID,
                )
                await cp.call(boot_req)
                for cid in model.connectors.keys():
                    await send_status(cid)
                # send connector 0 status to mimic real chargers
                root_status = call.StatusNotificationPayload(
                    connector_id=0,
                    error_code="NoError",
                    status=EVSEState.AVAILABLE,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )
                await cp.call(root_status)

                # tasks: heartbeat, metering
                hb_task = asyncio.create_task(send_heartbeat_loop())
                mv_task = asyncio.create_task(send_meter_loop())
                await asyncio.gather(hb_task, mv_task)
        except Exception as e:
            logging.error(f"OCPP client error: {e}")
            await asyncio.sleep(5)

async def send_heartbeat_loop():
    while True:
        try:
            req = call.HeartbeatPayload()
            await cp.call(req)  # type: ignore
        except Exception as e:
            logging.error(f"Heartbeat failed: {e}")
            return
        await asyncio.sleep(SEND_HEARTBEAT_SEC)

async def send_meter_loop():
    while True:
        t = datetime.now(timezone.utc).isoformat()
        for c in model.connectors.values():
            if not c.session_active:
                continue
            # เพิ่มพลังงาน (Wh) ตาม rate * period
            added_wh = int((METER_RATE_W * METER_PERIOD_SEC) / 3600)
            c.meter_wh += added_wh

            # base values for measurands
            base_voltage = 230.0
            base_power = float(METER_RATE_W)
            base_current = base_power / base_voltage

            # apply small random deltas
            current_a = base_current + random.uniform(-1.0, 1.0)
            voltage_v = base_voltage + random.uniform(-1.0, 1.0)
            power_w = base_power + random.uniform(-100.0, 100.0)
            temp_c = 28.0 + random.uniform(-0.5, 0.5)
            soc = 0.0

            energy_kwh = c.meter_wh / 1000

            sampled = [
                {
                    "value": f"{energy_kwh:.3f}",
                    "context": "Sample.Clock",
                    "format": "Raw",
                    "measurand": "Energy.Active.Import.Register",
                    "location": "Body",
                    "unit": "kWh",
                },
                {
                    "value": f"{current_a:.2f}",
                    "context": "Sample.Clock",
                    "format": "Raw",
                    "measurand": "Current.Import",
                    "location": "Body",
                    "unit": "A",
                },
                {
                    "value": f"{voltage_v:.1f}",
                    "context": "Sample.Clock",
                    "format": "Raw",
                    "measurand": "Voltage",
                    "location": "Body",
                    "unit": "V",
                },
                {
                    "value": f"{power_w/1000:.1f}",
                    "context": "Sample.Clock",
                    "format": "Raw",
                    "measurand": "Power.Active.Import",
                    "location": "Body",
                    "unit": "kW",
                },
                {
                    "value": f"{soc:.0f}",
                    "context": "Sample.Clock",
                    "format": "Raw",
                    "measurand": "SoC",
                    "location": "EV",
                    "unit": "Percent",
                },
                {
                    "value": f"{temp_c:.1f}",
                    "context": "Sample.Clock",
                    "format": "Raw",
                    "measurand": "Temperature",
                    "location": "Outlet",
                    "unit": "Celsius",
                },
            ]
            mv = [{"timestamp": t, "sampledValue": sampled}]

            req = call.MeterValuesPayload(connector_id=c.id, meter_value=mv)
            await cp.call(req)  # type: ignore
            logging.info(
                "MeterValues: cid=%s, energy(kWh)=%.3f, current(A)=%.2f, voltage(V)=%.1f, power(kW)=%.1f",
                c.id,
                energy_kwh,
                current_a,
                voltage_v,
                power_w / 1000,
            )
        await asyncio.sleep(METER_PERIOD_SEC)

# -------- HTTP control for simulating plug/unplug & local start/stop --------
@app.post("/plug/{connector_id}")
async def plug(connector_id: int):
    c = model.get(connector_id)
    c.plugged = True
    c.state = EVSEState.PREPARING
    await send_status(connector_id)
    return {"ok": True, "connector": connector_id, "plugged": True}

@app.post("/unplug/{connector_id}")
async def unplug(connector_id: int):
    c = model.get(connector_id)
    c.plugged = False
    if c.tx_id is not None:
        model.clear_tx(c.tx_id)
    c.state = EVSEState.AVAILABLE
    c.id_tag = None
    await send_status(connector_id)
    return {"ok": True, "connector": connector_id, "plugged": False}

@app.post("/local_start/{connector_id}")
async def local_start(connector_id: int, id_tag: str = "LOCAL_TAG"):
    c = model.get(connector_id)
    if not c.plugged:
        return {"ok": False, "error": "not plugged"}
    await start_local(connector_id, id_tag)
    return {"ok": True}

@app.post("/local_stop/{connector_id}")
async def local_stop(connector_id: int):
    c = model.get(connector_id)
    if not c.session_active:
        return {"ok": False, "error": "no active session"}
    await stop_local_by_tx(c.tx_id, c.meter_wh)  # type: ignore
    return {"ok": True}

# -------- fault / suspend injection --------

@app.post("/fault/{connector_id}")
async def inject_fault(connector_id: int, error_code: str = "OtherError"):
    c = model.set_fault(connector_id, error_code)
    await send_status(connector_id)
    return {"ok": True, "connector": connector_id, "error_code": c.error_code}

@app.post("/clear_fault/{connector_id}")
async def clear_fault(connector_id: int):
    model.clear_fault(connector_id)
    await send_status(connector_id)
    return {"ok": True, "connector": connector_id}

@app.post("/suspend_ev/{connector_id}")
async def suspend_ev(connector_id: int):
    model.set_state(connector_id, EVSEState.SUSPENDED_EV)
    await send_status(connector_id)
    return {"ok": True, "connector": connector_id, "state": EVSEState.SUSPENDED_EV}

@app.post("/suspend_evse/{connector_id}")
async def suspend_evse(connector_id: int):
    model.set_state(connector_id, EVSEState.SUSPENDED_EVSE)
    await send_status(connector_id)
    return {"ok": True, "connector": connector_id, "state": EVSEState.SUSPENDED_EVSE}

@app.post("/resume/{connector_id}")
async def resume(connector_id: int):
    model.set_state(connector_id, EVSEState.AVAILABLE)
    await send_status(connector_id)
    return {"ok": True, "connector": connector_id, "state": EVSEState.AVAILABLE}

async def main():
    # run OCPP client and HTTP API together
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=HTTP_PORT, loop="asyncio", log_level="info"))
    api_task = asyncio.create_task(server.serve())
    await ocpp_client()
    api_task.cancel()

if __name__ == "__main__":
    asyncio.run(main())