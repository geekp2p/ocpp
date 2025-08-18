# windows_fw_diagnose.py
# ตรวจ Windows Defender Firewall + ทดสอบการเชื่อมต่อ TCP/HTTP สำหรับพอร์ตที่สนใจ
# ทดสอบบน Windows Server 2022/2025 และ Windows 10/11
import argparse
import ctypes
import json
import platform
import socket
import subprocess
import sys
import time

TARGET_DEFAULT_IP = "45.136.236.186"
TARGET_DEFAULT_PORT = 8080
TARGET_DEFAULT_PATH = "/api/v1/health"

def is_windows() -> bool:
    return platform.system().lower() == "windows"

def is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0  # type: ignore[attr-defined]
    except Exception:
        return False

def run_ps(ps_script: str) -> str:
    # เรียก PowerShell และคืน stdout (string), raise หาก error
    cmd = [
        "powershell",
        "-NoLogo",
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-Command", ps_script
    ]
    return subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT)

def try_json(s: str):
    s = s.strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return None

def get_fw_profiles():
    ps = r"Get-NetFirewallProfile | Select-Object Name,Enabled,DefaultInboundAction,DefaultOutboundAction | ConvertTo-Json"
    out = run_ps(ps)
    data = try_json(out)
    if isinstance(data, dict):
        return [data]
    return data or []

def get_inbound_rules_for_port(port: int):
    # ดึงรายการ Rule ฝั่ง Inbound ที่เกี่ยวกับพอร์ตนี้ (TCP)
    ps = rf"""
$results = @()
Get-NetFirewallRule -Enabled True -Direction Inbound | ForEach-Object {{
  $rule = $_
  try {{
    Get-NetFirewallPortFilter -AssociatedNetFirewallRule $rule |
      Where-Object {{ $_.Protocol -eq 'TCP' -and ( $_.LocalPort -eq 'Any' -or $_.LocalPort -match '{port}' ) }} |
      ForEach-Object {{
        $results += [PSCustomObject]@{{
          Name       = $rule.Name
          DisplayName= $rule.DisplayName
          Direction  = $rule.Direction
          Action     = $rule.Action
          Enabled    = $rule.Enabled
          Profile    = $rule.Profile
          Program    = $rule.Program
          LocalPort  = $_.LocalPort
          Protocol   = $_.Protocol
        }}
      }}
  }} catch {{}}
}}
$results | ConvertTo-Json -Depth 4
"""
    out = run_ps(ps)
    data = try_json(out)
    if isinstance(data, dict):
        return [data]
    return data or []

def get_outbound_rules_for_port(port: int):
    # ดึงรายการ Rule ฝั่ง Outbound ที่เกี่ยวกับพอร์ตนี้ (TCP)
    ps = rf"""
$results = @()
Get-NetFirewallRule -Enabled True -Direction Outbound | ForEach-Object {{
  $rule = $_
  try {{
    Get-NetFirewallPortFilter -AssociatedNetFirewallRule $rule |
      Where-Object {{ $_.Protocol -eq 'TCP' -and ( $_.RemotePort -eq 'Any' -or $_.RemotePort -match '{port}' ) }} |
      ForEach-Object {{
        $results += [PSCustomObject]@{{
          Name       = $rule.Name
          DisplayName= $rule.DisplayName
          Direction  = $rule.Direction
          Action     = $rule.Action
          Enabled    = $rule.Enabled
          Profile    = $rule.Profile
          Program    = $rule.Program
          RemotePort = $_.RemotePort
          Protocol   = $_.Protocol
        }}
      }}
  }} catch {{}}
}}
$results | ConvertTo-Json -Depth 4
"""
    out = run_ps(ps)
    data = try_json(out)
    if isinstance(data, dict):
        return [data]
    return data or []

def test_tcp_http(ip: str, port: int, path: str, timeout=5.0):
    res = {
        "tcp_connect_ok": False,
        "tcp_error": None,
        "http_sent": False,
        "http_bytes_read": 0,
        "http_error": None,
        "inferred_reset": False
    }
    try:
        with socket.create_connection((ip, port), timeout=timeout) as s:
            res["tcp_connect_ok"] = True
            # ส่ง HTTP GET อย่างง่าย
            req = f"GET {path} HTTP/1.1\r\nHost: {ip}\r\nConnection: close\r\n\r\n".encode("ascii", "ignore")
            try:
                s.sendall(req)
                res["http_sent"] = True
                total = 0
                s.settimeout(timeout)
                while True:
                    try:
                        chunk = s.recv(4096)
                    except ConnectionResetError as e:
                        # WinError 10054 มัก = RST
                        res["http_error"] = f"ConnectionResetError: {e}"
                        res["inferred_reset"] = True
                        break
                    except OSError as e:
                        res["http_error"] = f"OSError while recv: {e}"
                        # หากเป็น WinError 10054 ก็ถือว่า reset
                        if getattr(e, "winerror", None) == 10054:
                            res["inferred_reset"] = True
                        break
                    if not chunk:
                        break
                    total += len(chunk)
                res["http_bytes_read"] = total
            except ConnectionResetError as e:
                res["http_error"] = f"ConnectionResetError on send: {e}"
                res["inferred_reset"] = True
            except OSError as e:
                res["http_error"] = f"OSError on send: {e}"
                if getattr(e, "winerror", None) == 10054:
                    res["inferred_reset"] = True
    except OSError as e:
        res["tcp_error"] = f"{e}"
        # บางกรณี connect ถูก reset ระหว่าง handshake อาจได้ 10054 เช่นกัน
        if getattr(e, "winerror", None) == 10054:
            res["inferred_reset"] = True
    return res

def test_netconnection(ip: str, port: int):
    ps = rf"Test-NetConnection -ComputerName {ip} -Port {port} | Select-Object TcpTestSucceeded,RemoteAddress,RemotePort,PingSucceeded | ConvertTo-Json"
    out = run_ps(ps)
    return try_json(out)

def add_allow_inbound_rule(port: int, name: str):
    ps = rf"New-NetFirewallRule -DisplayName '{name}' -Direction Inbound -Action Allow -Protocol TCP -LocalPort {port}"
    return run_ps(ps)

def add_allow_outbound_rule(port: int, name: str):
    ps = rf"New-NetFirewallRule -DisplayName '{name}' -Direction Outbound -Action Allow -Protocol TCP -RemotePort {port}"
    return run_ps(ps)

def main():
    if not is_windows():
        print("This script is intended for Windows only.")
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Diagnose Windows Firewall and TCP/HTTP to a target port.")
    parser.add_argument("--ip", default=TARGET_DEFAULT_IP, help="Target IP")
    parser.add_argument("--port", type=int, default=TARGET_DEFAULT_PORT, help="Target TCP port")
    parser.add_argument("--path", default=TARGET_DEFAULT_PATH, help="HTTP path to GET after TCP connect")
    parser.add_argument("--fix", choices=["allow-in", "allow-out"], help="(Admin) Create Allow firewall rule for this port")
    args = parser.parse_args()

    print(f"[i] Running on Windows. Admin: {is_admin()}")
    print(f"[i] Target: {args.ip}:{args.port}  HTTP GET {args.path}\n")

    # 1) ทดสอบ TCP + HTTP
    print("=== TCP/HTTP Probe ===")
    probe = test_tcp_http(args.ip, args.port, args.path)
    print(json.dumps(probe, indent=2))
    print()

    # 2) Test-NetConnection (ช่วยยืนยันว่า handshake TCP ผ่านหรือไม่)
    print("=== Test-NetConnection ===")
    try:
        tnc = test_netconnection(args.ip, args.port)
        print(json.dumps(tnc, indent=2))
    except subprocess.CalledProcessError as e:
        print("PowerShell error:", e.output)
        tnc = None
    print()

    # 3) ดึงโปรไฟล์ไฟร์วอลล์
    print("=== Firewall Profiles ===")
    try:
        profiles = get_fw_profiles()
        print(json.dumps(profiles, indent=2))
    except subprocess.CalledProcessError as e:
        print("PowerShell error:", e.output)
        profiles = []
    print()

    # 4) หา Inbound/Outbound rules ที่แตะพอร์ตนี้
    print(f"=== Inbound Rules touching TCP {args.port} ===")
    try:
        inbound = get_inbound_rules_for_port(args.port)
        print(json.dumps(inbound, indent=2))
        # นับ block/allow
        blocks = [r for r in inbound or [] if (r.get("Action") or "").lower() == "block"]
        allows = [r for r in inbound or [] if (r.get("Action") or "").lower() == "allow"]
        print(f"Summary: inbound allow={len(allows)}, block={len(blocks)}")
    except subprocess.CalledProcessError as e:
        print("PowerShell error:", e.output)
        inbound = []
    print()

    print(f"=== Outbound Rules touching TCP {args.port} ===")
    try:
        outbound = get_outbound_rules_for_port(args.port)
        print(json.dumps(outbound, indent=2))
        blocks = [r for r in outbound or [] if (r.get("Action") or "").lower() == "block"]
        allows = [r for r in outbound or [] if (r.get("Action") or "").lower() == "allow"]
        print(f"Summary: outbound allow={len(allows)}, block={len(blocks)}")
    except subprocess.CalledProcessError as e:
        print("PowerShell error:", e.output)
        outbound = []
    print()

    # 5) (ตัวเลือก) สร้าง Allow rule อัตโนมัติ
    if args.fix:
        if not is_admin():
            print("[!] --fix ต้องรัน PowerShell/Terminal แบบ Run as Administrator")
            sys.exit(2)
        try:
            if args.fix == "allow-in":
                print(f"[+] Creating Inbound Allow rule for TCP {args.port} ...")
                out = add_allow_inbound_rule(args.port, f"Allow TCP {args.port} Inbound (Auto)")
            else:
                print(f"[+] Creating Outbound Allow rule for TCP {args.port} ...")
                out = add_allow_outbound_rule(args.port, f"Allow TCP {args.port} Outbound (Auto)")
            print(out)
        except subprocess.CalledProcessError as e:
            print("[!] Failed to create rule. Error output:")
            print(e.output)

    # 6) สรุปเบื้องต้น
    print("=== Quick Hints ===")
    if probe["tcp_connect_ok"] and probe["inferred_reset"]:
        print("* TCP ต่อได้ แต่ถูก RST/รีเซ็ตระหว่างส่ง/รับ HTTP -> มักเป็นปัญหาที่ปลายทาง (service ปิด/ล่ม) หรือ firewall/IDS ตัดกลางทาง")
    elif not probe["tcp_connect_ok"]:
        print("* TCP ต่อไม่ติด -> ตรวจว่า service ปลายทางฟังพอร์ตนี้จริงหรือไม่ และ firewall ระหว่างทางบล็อกหรือไม่")
    else:
        print("* TCP/HTTP อ่านข้อมูลได้บางส่วนหรือทั้งหมด — ดูจำนวนไบต์และ response ที่ได้ (ถ้ามี)")

    print("\nDone.")

if __name__ == "__main__":
    main()

# python .\windows_fw_diagnose.py --ip 45.136.236.186 --port 8080 --path /api/v1/health
# python .\windows_fw_diagnose.py --ip 45.136.236.186 --port 8080 --fix allow-in
# python .\windows_fw_diagnose.py --ip 45.136.236.186 --port 8080 --fix allow-out
