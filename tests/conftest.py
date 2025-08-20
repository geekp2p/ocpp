import asyncio
import os
import sys
from pathlib import Path
import importlib

import pytest
import pytest_asyncio
import httpx
import websockets
from ocpp.routing import on
from ocpp.v16 import call, call_result, ChargePoint as CP
from ocpp.v16.enums import (
    Action,
    AuthorizationStatus,
    RegistrationStatus,
)

sys.path.append(str(Path(__file__).resolve().parents[1]))


class MockCSMS(CP):
    """Minimal CSMS that records start/stop requests and can send remote commands."""

    def __init__(self, id, websocket):
        super().__init__(id, websocket)
        self.start_requests: asyncio.Queue = asyncio.Queue()
        self.stop_requests: asyncio.Queue = asyncio.Queue()
        self.boot_notifications: asyncio.Queue = asyncio.Queue()

    # ---- handlers for messages from EVSE ----
    @on(Action.BootNotification)
    async def on_boot(self, charge_point_model, charge_point_vendor, **kwargs):
        await self.boot_notifications.put(
            {
                "charge_point_model": charge_point_model,
                "charge_point_vendor": charge_point_vendor,
            }
        )
        return call_result.BootNotificationPayload(
            current_time="0", interval=10, status=RegistrationStatus.accepted
        )

    @on(Action.Heartbeat)
    async def on_heartbeat(self, **kwargs):
        return call_result.HeartbeatPayload(current_time="0")

    @on(Action.StatusNotification)
    async def on_status(self, **kwargs):
        return call_result.StatusNotificationPayload()

    @on(Action.MeterValues)
    async def on_meter_values(self, **kwargs):
        return call_result.MeterValuesPayload()

    @on(Action.StartTransaction)
    async def on_start(self, connector_id, id_tag, meter_start, timestamp, **kwargs):
        await self.start_requests.put({"connector_id": connector_id, "id_tag": id_tag})
        return call_result.StartTransactionPayload(
            transaction_id=1,
            id_tag_info={"status": AuthorizationStatus.accepted},
        )

    @on(Action.StopTransaction)
    async def on_stop(self, transaction_id, meter_stop, timestamp, **kwargs):
        await self.stop_requests.put({"transaction_id": transaction_id})
        return call_result.StopTransactionPayload(
            id_tag_info={"status": AuthorizationStatus.accepted}
        )

    # ---- helpers for remote commands ----
    async def remote_start(self, *, id_tag: str, connector_id: int = 1):
        req = call.RemoteStartTransactionPayload(id_tag=id_tag, connector_id=connector_id)
        return await self.call(req)

    async def remote_stop(self, *, transaction_id: int):
        req = call.RemoteStopTransactionPayload(transaction_id=transaction_id)
        return await self.call(req)


class CSMS:
    """WebSocket server that accepts EVSE connections and exposes MockCSMS."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self.host = host
        self.port = port
        self.connected: asyncio.Event = asyncio.Event()
        self.cp: MockCSMS | None = None
        self.server = None

    async def start(self):
        async def on_connect(ws):
            self.cp = MockCSMS("CSMS", ws)
            self.connected.set()
            await self.cp.start()

        self.server = await websockets.serve(
            on_connect, self.host, self.port, subprotocols=["ocpp1.6"]
        )
        # store the actual port in case an ephemeral port was requested
        if self.port == 0 and self.server.sockets:
            self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self):
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}/ocpp"


@pytest_asyncio.fixture
async def simulator():
    """Spin up the EVSE simulator along with a mock CSMS."""
    csms = CSMS()
    await csms.start()

    os.environ["CSMS_URL"] = csms.url

    # import after setting env vars so config picks them up
    evse = importlib.import_module("sim.evse")

    ocpp_task = asyncio.create_task(evse.ocpp_client())

    await csms.connected.wait()

    transport = httpx.ASGITransport(app=evse.app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    try:
        yield {"csms": csms, "client": client}
    finally:
        await client.aclose()
        ocpp_task.cancel()
        try:
            await asyncio.wait_for(ocpp_task, timeout=1)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        await csms.stop()