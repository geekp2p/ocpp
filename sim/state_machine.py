from typing import Dict, Optional

class EVSEState:
    AVAILABLE = "Available"
    PREPARING = "Preparing"
    CHARGING = "Charging"
    FINISHING = "Finishing"
    FAULTED = "Faulted"
    SUSPENDED_EV = "SuspendedEV"
    SUSPENDED_EVSE = "SuspendedEVSE"
    OCCUPIED = "Occupied"

class ConnectorSim:
    def __init__(self, connector_id: int, meter_start_wh: int = 0):
        self.id = connector_id
        self.state = EVSEState.AVAILABLE
        self.plugged = False
        self.session_active = False
        self.id_tag = None
        self.meter_wh = meter_start_wh
        self.tx_id = None
        # keep track of the current OCPP error code so faults can be
        # injected and cleared via the HTTP API.
        self.error_code = "NoError"

    def to_status(self) -> str:
        # map internal -> OCPP status set
        if self.state == EVSEState.AVAILABLE:
            return "Available"
        if self.state == EVSEState.PREPARING:
            return "Preparing"
        if self.state == EVSEState.CHARGING:
            return "Charging"
        if self.state == EVSEState.FINISHING:
            return "Finishing"
        if self.state == EVSEState.FAULTED:
            return "Faulted"
        if self.state == EVSEState.SUSPENDED_EV:
            return "SuspendedEV"
        if self.state == EVSEState.SUSPENDED_EVSE:
            return "SuspendedEVSE"
        if self.state == EVSEState.OCCUPIED:
            return "Occupied"
        return "Available"

class EVSEModel:
    def __init__(self, connectors=1, meter_start_wh=0):
        self.connectors: Dict[int, ConnectorSim] = {
            i: ConnectorSim(i, meter_start_wh) for i in range(1, connectors + 1)
        }
        # map transaction_id -> connector_id for quick lookup
        self.tx_map: Dict[int, int] = {}

    def get(self, cid: int) -> ConnectorSim:
        return self.connectors[cid]

    def get_by_tx(self, tx_id: int) -> Optional[ConnectorSim]:
        cid = self.tx_map.get(tx_id)
        if cid is None:
            return None
        return self.connectors[cid]

    def assign_tx(self, cid: int, tx_id: int) -> None:
        """Register a transaction for a connector."""
        self.connectors[cid].tx_id = tx_id
        self.connectors[cid].session_active = True
        self.tx_map[tx_id] = cid

    def clear_tx(self, tx_id: int) -> Optional[ConnectorSim]:
        """Remove a transaction mapping and return the connector."""
        cid = self.tx_map.pop(tx_id, None)
        if cid is None:
            return None
        c = self.connectors[cid]
        c.tx_id = None
        c.session_active = False
        return c

    # ----- state / fault helpers -----
    def set_state(self, cid: int, state: str) -> ConnectorSim:
        c = self.get(cid)
        c.state = state
        return c

    def set_fault(self, cid: int, error_code: str) -> ConnectorSim:
        c = self.get(cid)
        c.state = EVSEState.FAULTED
        c.error_code = error_code
        return c

    def clear_fault(self, cid: int) -> ConnectorSim:
        c = self.get(cid)
        c.error_code = "NoError"
        # when a fault is cleared we treat the connector as Available
        c.state = EVSEState.AVAILABLE
        return c