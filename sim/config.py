import os

CSMS_URL = os.getenv("CSMS_URL", "ws://127.0.0.1:9000/ocpp")
# TLS certificate configuration (optional)
TLS_CA_CERT = os.getenv("TLS_CA_CERT")
TLS_CLIENT_CERT = os.getenv("TLS_CLIENT_CERT")
TLS_CLIENT_KEY = os.getenv("TLS_CLIENT_KEY")

CPID = os.getenv("CPID", "TestCP01")
CONNECTORS = int(os.getenv("CONNECTORS", "1"))

# information used in BootNotification to mimic a real charger
CP_VENDOR = os.getenv("CP_VENDOR", "Gresgying")
CP_MODEL = os.getenv("CP_MODEL", "F3-EU180-CC")
CP_SERIAL_NUMBER = os.getenv("CP_SERIAL_NUMBER", "24090200430002")
FIRMWARE_VERSION = os.getenv("FIRMWARE_VERSION", "C2089_V2.9.0_FME01")
ICCID = os.getenv("ICCID", "0")

METER_START_WH = int(os.getenv("METER_START_WH", "0"))
METER_RATE_W = int(os.getenv("METER_RATE_W", "7000"))          # 7 kW
METER_PERIOD_SEC = int(os.getenv("METER_PERIOD_SEC", "10"))     # ส่งทุก 10s
SEND_HEARTBEAT_SEC = int(os.getenv("SEND_HEARTBEAT_SEC", "60")) # heartbeat
HTTP_PORT = int(os.getenv("HTTP_PORT", "7071"))
