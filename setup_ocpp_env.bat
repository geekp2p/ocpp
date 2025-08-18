@echo off
REM ────────────────────────────────────────────────────────────────
REM setup_ocpp_env.bat
REM เพิ่ม Miniconda ลง PATH ชั่วคราว แล้วรัน `conda env create`
REM ────────────────────────────────────────────────────────────────

REM ปรับให้ตรงกับที่ติดตั้งของคุณ
set "CONDA_ROOT=C:\ProgramData\Miniconda3"

echo.
echo [1/3] กำลังอัปเดต PATH ชั่วคราว เพื่อให้เรียก conda ได้...
set "PATH=%CONDA_ROOT%;%CONDA_ROOT%\Scripts;%CONDA_ROOT%\Library\bin;%PATH%"

echo [2/3] ตรวจสอบ conda version:
conda --version
if errorlevel 1 (
  echo.
  echo ERROR: ยังเรียก conda ไม่ได้! กรุณาตรวจสอบ CONDA_ROOT ให้ถูกต้อง
  pause
  exit /b 1
)

echo [3/3] สร้าง environment จาก environment.yml:
conda env create -f "%~dp0environment.yml"

if errorlevel 1 (
  echo.
  echo ERROR: การสร้าง environment ล้มเหลว!
  pause
  exit /b 1
)

echo.
echo สำเร็จ! รัน:
echo    conda activate ocpp-central
echo เพื่อเข้าใช้งาน environment
pause
