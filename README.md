# OCPP Central Server

โปรเจกต์นี้ประกอบด้วยตัวอย่าง **CSMS (Central System)** สำหรับโปรโตคอล OCPP 1.6 พร้อม HTTP API และเครื่องมือช่วยทดสอบ/ดีบักที่เกี่ยวข้อง

## โครงสร้างหลัก

- `central.py` – เซิร์ฟเวอร์ WebSocket/HTTP API ที่รวมฟังก์ชัน Remote Start/Stop และคอนโซลคำสั่งเบื้องต้น
- `start_stop.go` – โค้ด Go ที่เรียก HTTP API `/api/v1/start`, `/charge/stop` หรือ `/api/v1/stop` เมื่อระบุ `transactionId`
- `list_active.go` – ตัวอย่าง Go สำหรับดึง `cpid`, `connectorId`, `idTag`, `transactionId` ของธุรกรรมที่กำลังชาร์จอยู่จาก `/api/v1/active`
- `list_active.py` – สคริปต์ Python สำหรับเรียกดู `cpid`, `connectorId`, `idTag`, `transactionId` ที่กำลังเชื่อมต่อ
- `cp_simulator.py` – ตัวจำลองหัวชาร์จอย่างง่ายสำหรับเชื่อมต่อทดสอบ
- `windows_fw_diagnose.py` – สคริปต์ PowerShell/Python สำหรับตรวจ/แก้ไข Windows Firewall

## เตรียมสภาพแวดล้อม

### Python
```bash
conda env create -f environment.yml  # หรือ pip install -r requirements.txt
conda activate ocpp-central
```

### Go (สำหรับทดสอบ start/stop)
ติดตั้ง Go 1.20 ขึ้นไป จากนั้นสามารถรัน/คอมไพล์ได้ด้วย
```bash
go run start_stop.go
# หรือ
go build start_stop.go
```

## การใช้งาน `central.py`

รันเซิร์ฟเวอร์
```bash
python central.py
```
เซิร์ฟเวอร์จะเปิด
- WebSocket OCPP1.6 ที่ `ws://<host>:9000/ocpp/<ChargePointID>`
- HTTP API ที่ `http://<host>:8080`

ตัวอย่างส่วนของ API:
- `POST /api/v1/start` ส่งคำสั่ง RemoteStartTransaction ให้หัวชาร์จที่เชื่อมต่ออยู่
- `POST /charge/stop` หยุดชาร์จโดยระบุ `cpid` และ `connectorId` (ไม่ต้องทราบ transactionId)
- `POST /api/v1/stop` หยุดชาร์จโดยส่ง `transactionId`
- `GET /api/v1/active` คืนรายการ `cpid`, `connectorId`, `idTag`, `transactionId` ที่กำลังมีธุรกรรมอยู่
ทุกเอ็นด์พอยต์ต้องใส่ header `X-API-Key` (ค่าเริ่มต้นคือ `changeme-123`).

บนคอนโซลที่รัน `central.py` สามารถสั่งได้ เช่น
```
start CP_001 1 TAG_1234  # เริ่มชาร์จ
stop CP_001 3           # หยุดชาร์จโดยใช้ transactionId 3
ls                      # แสดง CP ที่เชื่อมต่อ
```

## การใช้งาน `start_stop.go`

ไฟล์ Go นี้ใช้เรียก HTTP API `/api/v1/start`, `/charge/stop` หรือ `/api/v1/stop` จากระยะไกล โดยค่าเริ่มต้นจะชี้ไปยัง `http://45.136.236.186:8080` สามารถปรับ `apiBase` หรือ `apiKey` ในไฟล์ได้ตามต้องการ นอกจากนี้สคริปต์จะเพิ่ม timestamp ปัจจุบันและค่า hash แบบ SHA-256 ลงในคำขอโดยอัตโนมัติ เพื่อให้เซิร์ฟเวอร์ตรวจสอบความถูกต้องได้

ตัวอย่างคำสั่ง:
```bash
# เริ่มชาร์จด้วย cpid/connectorId (idTag และ transactionId เป็นออปชัน)
go run start_stop.go start <cpid> <connectorId> [idTag] [transactionId]

# หยุดชาร์จโดยระบุ cpid และ connectorId (หรือเพิ่ม idTag/transactionId)
go run start_stop.go stop <cpid> <connectorId> [idTag] [transactionId]
```
หากได้รับ `context deadline exceeded` แสดงว่าไม่สามารถเชื่อมต่อถึงเซิร์ฟเวอร์ (อาจเพราะเซิร์ฟเวอร์ไม่ทำงานหรือถูกไฟร์วอลล์บล็อก).

### รูปแบบข้อมูล/แฮช สำหรับคำสั่ง Start/Stop

ทั้ง `POST /api/v1/start` และ `POST /api/v1/stop` รองรับการตรวจสอบ `hash` โดยประกอบ canonical string ตามลำดับดังนี้:

```
1: <cpid>
2: <connectorId>
3: <idTag-or-'-'>
4: <transactionId-or-'-'>
5: <timestamp-or-'-'>     # ใช้รูปแบบ UNIX: unix:<sec[.frac]>
6: <vid-or-'-'>
7: <kv-or-'-'>            # key=value[,key=value]*  (ตัดคีย์ hash ออกตอนคำนวณ)
8: <hash-or-'-'>          # SHA-256 hex ของ canonical string
```

canonical string ที่นำไปคำนวณ hash คือ

```
<cpid>|<connectorId>|<idTag-or-'-'>|<transactionId-or-'-'>|<timestamp-or-'-'>|<vid-or-'-'>|<kv-or-'-'>
```

ค่าที่ไม่ส่งให้แทนด้วย `-` และ `kv` จะถูกเรียงตามชื่อ key (ละเว้น key `hash`) ก่อนนำมาประกอบ canonical string แล้วจึงคำนวณค่า `hash` ด้วย SHA-256.

## การใช้งาน `cp_simulator.py`

สคริปต์นี้จำลองหัวชาร์จ ID `CP_001` และเชื่อมต่อไปยังเซิร์ฟเวอร์ที่ `ws://45.136.236.186:9000/ocpp/CP_001` เพื่อใช้ทดสอบคำสั่ง Start/Stop จาก API
```bash
python cp_simulator.py
```
เมื่อเห็น log ว่าเชื่อมต่อสำเร็จแล้ว จึงค่อยเรียก `start_stop.go` หรือ HTTP API `/api/v1/start`, `/charge/stop` หรือ `/api/v1/stop`

## ตรวจสอบไฟร์วอลล์บน Windows

สำหรับ Windows Server 2022/2025 หรือ Windows 10/11 สามารถใช้สคริปต์ `windows_fw_diagnose.py` เพื่อเช็กหรือเปิดพอร์ตได้
```bash
python windows_fw_diagnose.py --ip 45.136.236.186 --port 8080 --path /api/v1/health
# เพิ่มกฎ Allow inbound
python windows_fw_diagnose.py --ip 45.136.236.186 --port 9000 --fix allow-in
```
หรือสร้างกฎด้วยตนเอง
```powershell
netsh advfirewall firewall add rule name="Allow OCPP 9000" dir=in action=allow protocol=TCP localport=9000
```

## หมายเหตุ
- เปลี่ยนค่า `API_KEY` ใน `central.py` และ `apiKey` ใน `start_stop.go` ก่อนใช้งานจริง␍