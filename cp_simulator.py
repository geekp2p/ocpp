import asyncio
import logging
from datetime import datetime

import websockets
from ocpp.routing import on
from ocpp.v16 import ChargePoint as CPBase
from ocpp.v16 import call, call_result
from ocpp.v16.enums import Action, AuthorizationStatus, RegistrationStatus, RemoteStartStopStatus

logging.basicConfig(level=logging.INFO)


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
