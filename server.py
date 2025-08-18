import asyncio
import logging
import threading
from datetime import datetime
import time
import json

import websockets
from fastapi import FastAPI, WebSocket, HTTPException
from fastapi.responses import HTMLResponse
from starlette.websockets import WebSocketDisconnect
import uvicorn

from ocpp.routing import on
from ocpp.v16 import ChargePoint as BaseChargePoint
from ocpp.v16 import call
from ocpp.v16 import call_result
from ocpp.v16.enums import RegistrationStatus, Action, Measurand, AuthorizationStatus, ChargePointStatus, RemoteStartStopStatus
from ocpp.exceptions import OCPPError

# Configure logging for more detailed output
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Central state for managing connected Charge Points ---
# Dictionary to store connected Charge Point objects, using ID as key
connected_charge_points = {}
# Set to store WebSocket clients for the dashboard
dashboard_clients = set()

# --- Mock configuration data for demonstration ---
CONFIGURATION_SETTINGS = {
    "AllowOfflineTxForUnknownId": {"value": "false", "readonly": False},
    "AuthorizationKey": {"value": "YourAuthKey", "readonly": True},
    "HeartbeatInterval": {"value": "10", "readonly": False},
    "ChargePointSerialNumber": {"value": "CP001-XYZ", "readonly": True},
    "MaxEnergyOnInvalidId": {"value": "60", "readonly": False},
    "SupportedFeatureProfiles": {"value": "Core,SmartCharging,FirmwareManagement", "readonly": True}
}

# --- Mock list of authorized ID tags ---
AUTHORIZED_ID_TAGS = ['TAG_1234', 'TAG_AABBCC']

# --- Auto-stop config (low power sustained) ---
AUTO_STOP_CFG = {
    "enabled": True,
    "threshold_kw": 0.80,      # กำลังไฟต่ำกว่าเท่านี้ถือว่า "ต่ำ"
    "duration_sec": 180        # ต่ำต่อเนื่องกี่วินาทีถึงจะหยุด
}

# ติดตามช่วงเวลาที่ "ต่ำ" ต่อเนื่องสำหรับแต่ละหัวชาร์จระหว่างธุรกรรม
# key = (cp_id, connector_id) -> {"below_since": datetime|None}
LOW_POWER_TRACK = {}

# ------------------ FastAPI Dashboard ------------------
app = FastAPI()

@app.get("/", response_class=HTMLResponse)
async def get_dashboard():
    """
    Serves the HTML Dashboard for monitoring Charge Point status.
    The dashboard connects to a separate WebSocket for real-time updates.
    """
    html_content = """
    <!DOCTYPE html>
    <html lang="th">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>OCPP Dashboard</title>
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;700&display=swap" rel="stylesheet">
        <script src="https://cdn.tailwindcss.com"></script>
        <style>
            body {
                font-family: 'Inter', sans-serif;
                background-color: #F0F4F8; /* Lighter background */
                color: #1E293B; /* Darker text for contrast */
            }
            .container {
                max-width: 1200px;
                margin: 0 auto;
                padding: 16px;
            }
            .title {
                color: #1E3A8A; /* Dark blue title */
                font-size: 2.25rem;
                font-weight: 700;
                text-align: center;
                margin-bottom: 2rem;
            }
            .charge-point-card, .config-card {
                background-color: #FFFFFF; /* White cards */
                border-radius: 1.5rem; /* Increased border radius */
                padding: 2rem;
                box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.1), 0 8px 10px -6px rgba(0, 0, 0, 0.05);
                transition: transform 0.3s ease-in-out, box-shadow 0.3s ease-in-out;
            }
            .charge-point-card:hover {
                transform: translateY(-8px);
                box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.25);
            }
            .config-card {
                padding: 1.5rem;
                border-radius: 0.75rem;
            }
            #messages {
                background-color: #FFFFFF;
                border-radius: 0.75rem;
                padding: 1.5rem;
                box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.1), 0 4px 6px -2px rgba(0, 0, 0, 0.05);
                height: 500px;
                overflow-y: scroll;
            }
            .msg {
                margin: 0.5rem 0;
                font-family: 'Courier New', Courier, monospace;
                color: #4B5563; /* Gray text for logs */
                border-bottom: 1px solid #E5E7EB;
                padding-bottom: 0.5rem;
            }
            .msg:last-child {
                border-bottom: none;
            }
            .control-btn {
                padding: 10px 20px;
                border-radius: 0.75rem; /* Rounded button corners */
                font-size: 14px;
                font-weight: 500;
                color: #fff;
                transition: all 0.2s ease;
            }
            .control-btn:hover {
                filter: brightness(1.1);
                transform: translateY(-2px);
                box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
            }
            .remove-btn { background-color: #EF4444; }  /* Red */
            .diagnostics-btn { background-color: #3B82F6; } /* Blue */
            .utility-btn { background-color: #EAB308; } /* Orange-yellow */
            .start-btn { background-color: #22C55E; } /* Green */
            .stop-btn { background-color: #EF4444; }  /* Red */
            .config-btn { background-color: #4F46E5; }  /* Purple */
            .remote-start-btn { background-color: #3B82F6; } /* Blue for Remote Start */
            .api-link {
                text-decoration: none;
            }

            .config-input {
                width: 100%;
                padding: 10px;
                border-radius: 0.5rem;
                background-color: #F8FAFC;
                border: 1px solid #CBD5E1;
                color: #1E293B;
                margin-bottom: 12px;
            }
            .config-input::placeholder {
                color: #94A3B8;
            }
            .status-indicator {
                width: 20px;
                height: 20px;
                border-radius: 50%;
                border: 2px solid #fff;
                box-shadow: 0 0 5px rgba(0, 0, 0, 0.2);
            }
            .status-online { background-color: #22c55e; } /* Bright green */
            .status-offline { background-color: #ef4444; } /* Red */
            .status-charging {
                background-color: #f97316; /* Orange */
                animation: pulse-orange 1.5s infinite ease-in-out;
            }
            .status-preparing {
                background-color: #eab308; /* Yellow */
                animation: pulse-yellow 1.5s infinite ease-in-out;
            }

            @keyframes pulse-orange {
                0%, 100% { box-shadow: 0 0 5px #f97316, 0 0 10px #f97316; }
                50% { box-shadow: 0 0 10px #f97316, 0 0 20px #f97316; }
            }
            @keyframes pulse-yellow {
                0%, 100% { box-shadow: 0 0 5px #eab308, 0 0 10px #eab308; }
                50% { box-shadow: 0 0 10px #eab308, 0 0 20px #eab308; }
            }
            @media (max-width: 1024px) {
                .charge-point-card {
                    padding: 1rem;
                }
                .control-btn {
                    width: 100%;
                }
            }
            @media (min-width: 768px) {
                .connector-actions {
                    flex-direction: row;
                }
            }
        </style>
    </head>
    <body class="bg-gray-100 flex items-center justify-center min-h-screen p-4">

        <div class="container">
            <div class="flex justify-between items-center mb-6">
                <h1 class="title">OCPP Dashboard</h1>
                <a href="/api" class="api-link">
                    <button class="control-btn config-btn">ดู API</button>
                </a>
            </div>

            <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
                <div>
                    <h2 class="text-xl font-bold mb-4 text-blue-900">สถานะ Charge Point</h2>
                    <div id="charge-points-list" class="space-y-4">
                        <p class="text-gray-500">กำลังโหลด Charge Point...</p>
                    </div>

                    <div class="mt-8 config-card">
                        <h2 class="text-xl font-bold mb-4 text-blue-900">Configuration Management</h2>
                        <input type="text" id="config-cp-id" placeholder="Charge Point ID" class="config-input">
                        <input type="text" id="config-key" placeholder="Configuration Key" class="config-input">
                        <input type="text" id="config-value" placeholder="New Value" class="config-input">
                        <button class="control-btn config-btn w-full mt-2" onclick="changeConfiguration()">Change Configuration</button>
                    </div>
                </div>

                <div id="live-logs-container">
                    <h2 class="text-xl font-bold mb-4 text-blue-900">Live Logs</h2>
                    <input type="text" id="log-filter" placeholder="ค้นหา Charge Point ID..." class="w-full mb-4 p-2 rounded-md bg-white border border-gray-300 text-gray-700 focus:outline-none focus:ring-2 focus:ring-blue-500">
                    <div id="messages" class="text-sm"></div>
                </div>
            </div>
        </div>

        <div id="remove-confirmation-modal" class="fixed inset-0 z-50 flex items-center justify-center bg-gray-900 bg-opacity-50 hidden">
            <div class="bg-white p-6 rounded-lg shadow-xl w-96">
                <p id="remove-modal-text" class="text-gray-800 text-lg mb-4"></p>
                <div class="flex justify-end space-x-4">
                    <button id="remove-modal-cancel-btn" class="bg-gray-200 hover:bg-gray-300 text-gray-800 font-bold py-2 px-4 rounded-md">ยกเลิก</button>
                    <button id="remove-modal-confirm-btn" class="bg-red-600 hover:bg-red-700 text-white font-bold py-2 px-4 rounded-md">ยืนยัน</button>
                </div>
            </div>
        </div>

        <div id="diagnostics-modal" class="fixed inset-0 z-50 flex items-center justify-center bg-gray-900 bg-opacity-50 hidden">
            <div class="bg-white p-6 rounded-lg shadow-xl w-96">
                <p class="text-gray-800 text-lg mb-4">ระบุ URL สำหรับอัปโหลดไฟล์ Diagnostics</p>
                <input type="text" id="diagnostics-location" placeholder="เช่น ftp://your-server.com/diagnostics/" class="w-full p-3 rounded-lg border border-gray-300 text-gray-800 focus:outline-none focus:ring-2 focus:ring-blue-500 mb-4">
                <div class="flex justify-end space-x-4">
                    <button id="diagnostics-cancel-btn" class="bg-gray-200 hover:bg-gray-300 text-gray-800 font-bold py-2 px-4 rounded-md">ยกเลิก</button>
                    <button id="diagnostics-confirm-btn" class="bg-blue-600 hover:bg-blue-700 text-white font-bold py-2 px-4 rounded-md">ส่งคำสั่ง</button>
                </div>
            </div>
        </div>

        <script>
            const dashboardWs = new WebSocket("ws://" + location.hostname + ":8000/ws/dashboard");
            const messagesBox = document.getElementById("messages");
            const chargePointsList = document.getElementById("charge-points-list");
            const logFilterInput = document.getElementById("log-filter");

            // Remove Confirmation Modal elements
            const removeConfirmationModal = document.getElementById('remove-confirmation-modal');
            const removeModalText = document.getElementById('remove-modal-text');
            const removeModalCancelBtn = document.getElementById('remove-modal-cancel-btn');
            const removeModalConfirmBtn = document.getElementById('remove-modal-confirm-btn');

            // Diagnostics Modal elements
            const diagnosticsModal = document.getElementById('diagnostics-modal');
            const diagnosticsLocationInput = document.getElementById('diagnostics-location');
            const diagnosticsCancelBtn = document.getElementById('diagnostics-cancel-btn');
            const diagnosticsConfirmBtn = document.getElementById('diagnostics-confirm-btn');
            
            // State variables
            let allLogs = [];
            let currentCpIdToRemove = null;
            let currentCpIdForDiagnostics = null;

            // WebSocket message handler
            dashboardWs.onmessage = function(event) {
                const logData = event.data;
                allLogs.push(logData);
                filterLogs();
            };

            // Event listeners
            logFilterInput.addEventListener('input', filterLogs);
            removeModalCancelBtn.addEventListener('click', () => removeConfirmationModal.classList.add('hidden'));
            diagnosticsCancelBtn.addEventListener('click', () => diagnosticsModal.classList.add('hidden'));

            removeModalConfirmBtn.addEventListener('click', async () => {
                if (currentCpIdToRemove) {
                    await removeChargePointFromAPI(currentCpIdToRemove);
                }
                removeConfirmationModal.classList.add('hidden');
                currentCpIdToRemove = null;
            });

            diagnosticsConfirmBtn.addEventListener('click', async () => {
                if (currentCpIdForDiagnostics) {
                    const location = diagnosticsLocationInput.value;
                    await getDiagnosticsFromAPI(currentCpIdForDiagnostics, location);
                }
                diagnosticsModal.classList.add('hidden');
                currentCpIdForDiagnostics = null;
                diagnosticsLocationInput.value = '';
            });

            // Filtering logs logic
            function filterLogs() {
                const filterText = logFilterInput.value.toLowerCase();
                messagesBox.innerHTML = ''; // Clear old logs
                const filteredLogs = allLogs.filter(log => log.toLowerCase().includes(filterText));

                filteredLogs.forEach(log => {
                    const div = document.createElement("div");
                    div.className = "msg";
                    div.innerText = log;
                    messagesBox.appendChild(div);
                });
                messagesBox.scrollTop = messagesBox.scrollHeight;
            }

            // Function to fetch and render Charge Points
            async function fetchAndRenderChargePoints() {
                try {
                    const response = await fetch('/api/charge_points');
                    const data = await response.json();
                    chargePointsList.innerHTML = '';
                    if (data.charge_points.length === 0) {
                        chargePointsList.innerHTML = '<p class="text-gray-500">ไม่มี Charge Point เชื่อมต่ออยู่</p>';
                        return;
                    }

                    data.charge_points.forEach(cp => {
                        const cpCard = document.createElement('div');
                        cpCard.className = 'charge-point-card w-full mb-8'; // ใช้ class ใหม่
                        cpCard.innerHTML = `
                            <div class="flex flex-col sm:flex-row justify-between items-start sm:items-center mb-6 border-b pb-4">
                                <h2 class="text-3xl font-bold text-blue-800">ID: ${cp.id}</h2>
                                <button class="control-btn remove-btn mt-4 sm:mt-0" onclick="showRemoveConfirmationModal('${cp.id}')">ลบ</button>
                            </div>

                            <div class="flex flex-wrap gap-4 mb-8">
                                <button class="control-btn diagnostics-btn flex-1" onclick="showDiagnosticsModal('${cp.id}')">Get Diagnostics</button>
                                <button class="control-btn utility-btn flex-1" onclick="clearCache('${cp.id}')">Clear Cache</button>
                            </div>

                            <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
                                </div>
                        `;

                        const connectorsContainer = cpCard.querySelector('.grid');
                        cp.connectors.forEach(conn => {
                            if (conn.connector_id === 0) {
                                return;
                            }

                            let statusClass;
                            switch(conn.status) {
                                case 'Available':
                                case 'Operative':
                                    statusClass = 'status-online';
                                    break;
                                case 'Preparing':
                                    statusClass = 'status-preparing';
                                    break;
                                case 'Charging':
                                    statusClass = 'status-charging';
                                    break;
                                case 'Offline':
                                case 'Unavailable':
                                case 'Inoperative':
                                default:
                                    statusClass = 'status-offline';
                            }

                            const connDiv = document.createElement('div');
                            connDiv.className = 'bg-blue-50 border border-blue-200 rounded-xl p-6 flex flex-col items-start space-y-4';
                            connDiv.innerHTML = `
                                <div class="flex items-center gap-4">
                                    <div class="status-indicator w-5 h-5 rounded-full ${statusClass}"></div>
                                    <div>
                                        <p class="text-xl font-bold text-blue-900">หัวชาร์จ: ${conn.connector_id}</p>
                                        <p class="text-sm text-gray-600">สถานะ: ${conn.status}</p>
                                        ${conn.power_kw !== null ? `<p class="text-sm text-gray-600">กำลังไฟ: ${conn.power_kw.toFixed(2)} kW</p>` : ''}
                                        ${conn.transaction_id !== null ? `<p class="text-sm text-gray-600">ธุรกรรม ID: ${conn.transaction_id}</p>` : ''}
                                    </div>
                                </div>

                                <input type="text" id="idTag-${cp.id}-${conn.connector_id}" placeholder="ID Tag" class="w-full p-3 rounded-lg border border-gray-300 text-gray-800 focus:outline-none focus:ring-2 focus:ring-blue-500">

                                <div class="flex flex-wrap w-full gap-2 connector-actions">
                                    <button class="control-btn start-btn flex-1" onclick="localStart('${cp.id}', ${conn.connector_id})">Local Start</button>
                                    <button class="control-btn remote-start-btn flex-1" onclick="remoteStart('${cp.id}', ${conn.connector_id})">Remote Start</button>
                                </div>
                                <div class="flex flex-wrap w-full gap-2 connector-actions">
                                    <button class="control-btn stop-btn flex-1" onclick="remoteStop('${cp.id}', ${conn.connector_id}, ${conn.transaction_id})">Remote Stop</button>
                                    <button class="control-btn utility-btn flex-1" onclick="changeAvailability('${cp.id}', ${conn.connector_id}, 'Operative')">Set Operative</button>
                                    <button class="control-btn stop-btn flex-1" onclick="changeAvailability('${cp.id}', ${conn.connector_id}, 'Inoperative')">Set Inoperative</button>
                                </div>
                            `;
                            connectorsContainer.appendChild(connDiv);
                        });
                        chargePointsList.appendChild(cpCard);
                    });
                } catch (error) {
                    console.error('Error fetching charge points:', error);
                    const errorDiv = document.createElement("div");
                    errorDiv.className = "msg text-red-500";
                    errorDiv.innerText = `[Error] ไม่สามารถโหลด Charge Point ได้: ${error}`;
                    messagesBox.appendChild(errorDiv);
                    chargePointsList.innerHTML = '<p class="text-red-400">ไม่สามารถโหลด Charge Point ได้</p>';
                }
            }

            // Custom Modal Logic for removing a CP
            function showRemoveConfirmationModal(cpId) {
                currentCpIdToRemove = cpId;
                removeModalText.innerText = `คุณต้องการลบ Charge Point ID: ${cpId} ออกจากระบบหรือไม่?`;
                removeConfirmationModal.classList.remove('hidden');
            }

            // Custom Modal Logic for Get Diagnostics
            function showDiagnosticsModal(cpId) {
                currentCpIdForDiagnostics = cpId;
                diagnosticsModal.classList.remove('hidden');
            }
            
            // API interaction functions
            async function getDiagnosticsFromAPI(cpId, location) {
                try {
                    const response = await fetch('/api/get_diagnostics', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ charge_point_id: cpId, location: location })
                    });
                    const result = await response.json();
                    if (!response.ok) {
                        logError(`Error requesting diagnostics: ${result.detail}`);
                    } else {
                        logSuccess(`คำขอ Get Diagnostics ถูกส่งแล้ว: ${JSON.stringify(result)}`);
                    }
                } catch (error) {
                    logError('ไม่สามารถส่งคำสั่ง Get Diagnostics ได้');
                }
            }

            async function removeChargePointFromAPI(cpId) {
                try {
                    const response = await fetch('/api/remove_charge_point', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ charge_point_id: cpId })
                    });
                    const result = await response.json();
                    if (!response.ok) {
                        logError(`เกิดข้อผิดพลาดในการลบ Charge Point: ${result.detail}`);
                    } else {
                        logSuccess(`[Success] ${result.message}`);
                        fetchAndRenderChargePoints(); // Refresh the list
                    }
                } catch (error) {
                    logError('ไม่สามารถส่งคำสั่งลบได้');
                }
            }

            async function localStart(cpId, connectorId) {
                const idTag = document.getElementById(`idTag-${cpId}-${connectorId}`).value;
                if (!idTag) {
                    logError("กรุณาใส่ ID Tag เพื่อเริ่มชาร์จ");
                    return;
                }

                try {
                    const response = await fetch('/api/local_start_transaction', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ charge_point_id: cpId, connector_id: connectorId, id_tag: idTag })
                    });
                    const result = await response.json();
                    if (!response.ok) {
                        logError(`Error starting transaction: ${result.detail}`);
                    } else {
                        logSuccess(`คำขอ Local Start ถูกส่งแล้ว: ${JSON.stringify(result)}`);
                    }
                } catch (error) {
                    logError('ไม่สามารถส่งคำสั่ง local start ได้');
                }
            }

            async function remoteStart(cpId, connectorId) {
                const idTag = document.getElementById(`idTag-${cpId}-${connectorId}`).value;
                if (!idTag) {
                    logError("กรุณาใส่ ID Tag เพื่อเริ่มชาร์จจากระยะไกล");
                    return;
                }

                try {
                    const response = await fetch('/api/remote_start_transaction', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ charge_point_id: cpId, connector_id: connectorId, id_tag: idTag })
                    });
                    const result = await response.json();
                    if (!response.ok) {
                        logError(`Error starting transaction remotely: ${result.detail}`);
                    } else {
                        logSuccess(`คำขอ Remote Start ถูกส่งแล้ว: ${JSON.stringify(result)}`);
                    }
                } catch (error) {
                    logError('ไม่สามารถส่งคำสั่ง remote start ได้');
                }
            }

            async function remoteStop(cpId, connectorId, transactionId) {
                if (transactionId === null || transactionId === undefined) {
                    logError('ไม่มีธุรกรรมที่ใช้งานอยู่สำหรับหัวชาร์จนี้');
                    return;
                }
                try {
                    const response = await fetch('/api/remote_stop_transaction', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ charge_point_id: cpId, transaction_id: transactionId })
                    });
                    const result = await response.json();
                    if (!response.ok) {
                        logError(`Error stopping transaction: ${result.detail}`);
                    } else {
                        logSuccess(`คำขอ Remote Stop ถูกส่งแล้ว: ${JSON.stringify(result)}`);
                    }
                } catch (error) {
                    logError('ไม่สามารถส่งคำสั่ง remote stop ได้');
                }
            }

            async function changeAvailability(cpId, connectorId, availabilityType) {
                try {
                    const response = await fetch('/api/change_availability', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ charge_point_id: cpId, connector_id: connectorId, type: availabilityType })
                    });
                    const result = await response.json();
                    if (!response.ok) {
                        logError(`Error changing availability: ${result.detail}`);
                    } else {
                        logSuccess(`คำขอ Change Availability ถูกส่งแล้ว: ${JSON.stringify(result)}`);
                    }
                } catch (error) {
                    logError('ไม่สามารถส่งคำสั่ง change availability ได้');
                }
            }

            async function clearCache(cpId) {
                try {
                    const response = await fetch('/api/clear_cache', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ charge_point_id: cpId })
                    });
                    const result = await response.json();
                    if (!response.ok) {
                        logError(`Error clearing cache: ${result.detail}`);
                    } else {
                        logSuccess(`คำขอ Clear Cache ถูกส่งแล้ว: ${JSON.stringify(result)}`);
                    }
                } catch (error) {
                    logError('ไม่สามารถส่งคำสั่ง clear cache ได้');
                }
            }

            async function changeConfiguration() {
                const cpId = document.getElementById('config-cp-id').value;
                const key = document.getElementById('config-key').value;
                const value = document.getElementById('config-value').value;

                if (!cpId || !key || !value) {
                    logError("กรุณาใส่ Charge Point ID, Key, และ Value ให้ครบถ้วน");
                    return;
                }

                try {
                    const response = await fetch('/api/change_configuration', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ charge_point_id: cpId, key: key, value: value })
                    });
                    const result = await response.json();
                    if (!response.ok) {
                        logError(`Error changing configuration: ${result.detail}`);
                    } else {
                        logSuccess(`คำขอ Change Configuration ถูกส่งแล้ว: ${JSON.stringify(result)}`);
                    }
                } catch (error) {
                    logError('ไม่สามารถส่งคำสั่ง change configuration ได้');
                }
            }

            // Helper functions for logging to the UI
            function logSuccess(message) {
                const div = document.createElement("div");
                div.className = "msg text-green-500";
                div.innerText = `[Success] ${message}`;
                messagesBox.appendChild(div);
                messagesBox.scrollTop = messagesBox.scrollHeight;
            }

            function logError(message) {
                const div = document.createElement("div");
                div.className = "msg text-red-500";
                div.innerText = `[Error] ${message}`;
                messagesBox.appendChild(div);
                messagesBox.scrollTop = messagesBox.scrollHeight;
            }

            // Global functions for onclick events
            window.showRemoveConfirmationModal = showRemoveConfirmationModal;
            window.showDiagnosticsModal = showDiagnosticsModal;
            window.localStart = localStart;
            window.remoteStart = remoteStart;
            window.remoteStop = remoteStop;
            window.changeAvailability = changeAvailability;
            window.clearCache = clearCache;
            window.changeConfiguration = changeConfiguration;

            // Fetch and render initial data, then refresh every 5 seconds
            fetchAndRenderChargePoints();
            setInterval(fetchAndRenderChargePoints, 5000);
        </script>

    </body>
    </html>
    """
    return HTMLResponse(html_content)

@app.get("/api", response_class=HTMLResponse)
async def get_api_docs():
    """
    Serves a dedicated HTML page for API documentation.
    """
    html_content = """
    <!DOCTYPE html>
    <html lang="th">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>OCPP API Documentation</title>
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;700&display=swap" rel="stylesheet">
        <script src="https://cdn.tailwindcss.com"></script>
        <style>
            body {
                font-family: 'Inter', sans-serif;
                background-color: #F0F4F8; /* Lighter background */
                color: #1E293B; /* Darker text for contrast */
                padding: 2rem;
            }
            .container {
                max-width: 1200px; /* Make it wider to match the dashboard */
                margin: 0 auto;
                background-color: #FFFFFF; /* White background */
                padding: 2rem;
                border-radius: 1.5rem; /* Match dashboard cards */
                box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.1), 0 8px 10px -6px rgba(0, 0, 0, 0.05); /* Match dashboard cards */
            }
            .title {
                color: #1E3A8A; /* Dark blue title */
                font-size: 2.5rem;
                font-weight: 700;
                margin-bottom: 0.5rem;
            }
            .subtitle {
                font-size: 1.25rem;
                color: #4B5563;
                margin-bottom: 2rem;
            }
            .api-section {
                margin-bottom: 2.5rem;
                padding-bottom: 1.5rem;
                border-bottom: 1px solid #E5E7EB;
            }
            .api-section:last-child {
                border-bottom: none;
            }
            .api-title {
                font-size: 1.75rem;
                font-weight: 700;
                color: #1E3A8A;
                margin-bottom: 0.5rem;
            }
            .api-endpoint {
                font-family: 'Courier New', Courier, monospace;
                background-color: #E2E8F0; /* Light gray */
                padding: 0.5rem 1rem;
                border-radius: 0.5rem;
                display: inline-block;
                margin-bottom: 1rem;
                color: #1E3A8A; /* Dark blue */
            }
            pre {
                background-color: #f9fafb; /* Lighter background to match dashboard logs */
                color: #1E293B;
                padding: 1rem;
                border-radius: 0.5rem;
                overflow-x: auto;
                border: 1px solid #E5E7EB;
                font-size: 0.9rem;
            }
            .back-btn {
                padding: 10px 20px;
                border-radius: 0.75rem;
                font-size: 14px;
                font-weight: 500;
                color: #fff;
                background-color: #3B82F6; /* Blue to match dashboard buttons */
                text-decoration: none;
                transition: all 0.2s ease;
            }
            .back-btn:hover {
                filter: brightness(1.1);
                transform: translateY(-2px);
                box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
            }
        </style>
    </head>
    <body>
        <div class="container">
            <a href="/" class="back-btn">← กลับสู่ Dashboard</a>
            <h1 class="title">OCPP API Documentation</h1>
            <p class="subtitle">รายการ API สำหรับการควบคุมและดูสถานะ Charge Point</p>
            
            <div class="api-section">
                <h3 class="api-title">Get Charge Points Data</h3>
                <p class="text-gray-600">ดึงข้อมูลสถานะทั้งหมดของ Charge Point ที่เชื่อมต่ออยู่</p>
                <span class="api-endpoint">GET /api/charge_points</span>
                <p class="font-bold text-gray-700 mt-4">ตัวอย่าง Response:</p>
                <pre><code>[
{
"id": "CP001",
"connectors": [
{
"connector_id": 1,
"status": "Available",
"power_kw": 0.0,
"last_heard": "2025-08-04T12:00:00.000Z",
"transaction_id": null
}
]
}
]</code></pre>
            </div>
            
            <div class="api-section">
                <h3 class="api-title">Remote Start Transaction</h3>
                <p class="text-gray-600">ส่งคำสั่งเริ่มชาร์จระยะไกลไปยัง Charge Point</p>
                <span class="api-endpoint">POST /api/remote_start_transaction</span>
                <p class="font-bold text-gray-700 mt-4">ตัวอย่าง Body (JSON):</p>
                <pre><code>{
"charge_point_id": "CP001",
"connector_id": 1,
"id_tag": "TAG_1234"
}</code></pre>
            </div>
            
            <div class="api-section">
                <h3 class="api-title">Remote Stop Transaction</h3>
                <p class="text-gray-600">ส่งคำสั่งหยุดชาร์จระยะไกล</p>
                <span class="api-endpoint">POST /api/remote_stop_transaction</span>
                <p class="font-bold text-gray-700 mt-4">ตัวอย่าง Body (JSON):</p>
                <pre><code>{
"charge_point_id": "CP001",
"transaction_id": 56789
}</code></pre>
            </div>

            <div class="api-section">
                <h3 class="api-title">Change Availability</h3>
                <p class="text-gray-600">ส่งคำสั่งเปลี่ยนสถานะของหัวชาร์จ</p>
                <span class="api-endpoint">POST /api/change_availability</span>
                <p class="font-bold text-gray-700 mt-4">ตัวอย่าง Body (JSON):</p>
                <pre><code>{
"charge_point_id": "CP001",
"connector_id": 1,
"type": "Inoperative"
}</code></pre>
            </div>

             <div class="api-section">
                <h3 class="api-title">Change Configuration</h3>
                <p class="text-gray-600">ส่งคำสั่งเพื่อเปลี่ยนค่า configuration ของ Charge Point</p>
                <span class="api-endpoint">POST /api/change_configuration</span>
                <p class="font-bold text-gray-700 mt-4">ตัวอย่าง Body (JSON):</p>
                <pre><code>{
"charge_point_id": "CP001",
"key": "HeartbeatInterval",
"value": "30"
}</code></pre>
            </div>
            
            <div class="api-section">
                <h3 class="api-title">Get Diagnostics</h3>
                <p class="text-gray-600">ส่งคำสั่งเพื่อขอไฟล์ diagnostics จาก Charge Point</p>
                <span class="api-endpoint">POST /api/get_diagnostics</span>
                <p class="font-bold text-gray-700 mt-4">ตัวอย่าง Body (JSON):</p>
                <pre><code>{
"charge_point_id": "CP001",
"location": "ftp://your-server.com/diagnostics/"
}</code></pre>
            </div>
            
            <div class="api-section">
                <h3 class="api-title">Clear Cache</h3>
                <p class="text-gray-600">ส่งคำสั่งให้ Charge Point ล้างแคชข้อมูล</p>
                <span class="api-endpoint">POST /api/clear_cache</span>
                <p class="font-bold text-gray-700 mt-4">ตัวอย่าง Body (JSON):</p>
                <pre><code>{
"charge_point_id": "CP001"
}</code></pre>
            </div>

            <div class="api-section">
                <h3 class="api-title">Remove Charge Point</h3>
                <p class="text-gray-600">ลบ Charge Point ออกจากระบบอย่างถาวร</p>
                <span class="api-endpoint">POST /api/remove_charge_point</span>
                <p class="font-bold text-gray-700 mt-4">ตัวอย่าง Body (JSON):</p>
                <pre><code>{
"charge_point_id": "CP001"
}</code></pre>
            </div>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(html_content)

async def notify_all_dashboard_clients(message: str):
    """Helper function to send a message to all connected dashboard clients."""
    # Create a list from the set to avoid modification during iteration
    for client in list(dashboard_clients):
        try:
            await client.send_text(message)
        except WebSocketDisconnect:
            dashboard_clients.remove(client)

@app.websocket("/ws/dashboard")
async def websocket_dashboard(websocket: WebSocket):
    await websocket.accept()
    dashboard_clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        dashboard_clients.remove(websocket)

@app.get("/api/charge_points")
async def get_charge_points_data():
    """
    API endpoint to retrieve data for all Charge Points and their connectors.
    """
    cp_data = []
    for cp_id, cp_obj in connected_charge_points.items():
        connectors_data = []
        for conn_id, conn_info in cp_obj.connectors.items():
            connectors_data.append({
                "connector_id": conn_id,
                "status": conn_info['status'],
                "power_kw": conn_info['power_kw'],
                "last_heard": conn_info['last_heard'].isoformat(),
                "transaction_id": conn_info.get('transaction_id')
            })
        cp_data.append({
            "id": cp_id,
            "connectors": connectors_data
        })
    return {"charge_points": cp_data}

@app.post("/api/local_start_transaction")
async def local_start_transaction_api(data: dict):
    """
    API endpoint to simulate a local start from the Charge Point.
    """
    cp_id = data.get("charge_point_id")
    connector_id = data.get("connector_id")
    id_tag = data.get("id_tag")

    if not cp_id or connector_id is None or not id_tag:
        raise HTTPException(status_code=400, detail="ต้องมี charge_point_id, connector_id และ id_tag")

    if cp_id not in connected_charge_points:
        raise HTTPException(status_code=404, detail="ไม่พบ Charge Point")

    charge_point = connected_charge_points[cp_id]

    try:
        response = await charge_point.local_start_process(id_tag, int(connector_id))
        return {"status": "success", "response": response}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/charge/start")
async def charge_start(data: dict):
    """
    เริ่มชาร์จแบบง่าย
    body: {"charge_point_id":"CP001","connector_id":1,"id_tag":"TAG_1234"}
    """
    cp_id = data.get("charge_point_id")
    connector_id = data.get("connector_id")
    id_tag = data.get("id_tag")

    if not cp_id or connector_id is None or not id_tag:
        raise HTTPException(status_code=400, detail="ต้องมี charge_point_id, connector_id และ id_tag")

    if cp_id not in connected_charge_points:
        raise HTTPException(status_code=404, detail="ไม่พบ Charge Point")

    cp = connected_charge_points[cp_id]
    resp = await cp.remote_start_transaction(id_tag=id_tag, connector_id=int(connector_id))
    return {"status": "sent", "response": resp}


@app.post("/api/charge/stop")
async def charge_stop(data: dict):
    """
    หยุดชาร์จแบบง่าย
    body: {"charge_point_id":"CP001","connector_id":1}
    (ระบบจะหา transaction_id ที่กำลังวิ่งจากสถานะของหัวนั้นให้เอง)
    """
    cp_id = data.get("charge_point_id")
    connector_id = data.get("connector_id")

    if not cp_id or connector_id is None:
        raise HTTPException(status_code=400, detail="ต้องมี charge_point_id และ connector_id")

    if cp_id not in connected_charge_points:
        raise HTTPException(status_code=404, detail="ไม่พบ Charge Point")

    cp = connected_charge_points[cp_id]
    conn = cp.connectors.get(int(connector_id))
    if not conn or not conn.get("transaction_id"):
        raise HTTPException(status_code=409, detail="หัวชาร์จนี้ไม่มีธุรกรรมที่กำลังทำงานอยู่")

    tx_id = conn["transaction_id"]
    resp = await cp.remote_stop_transaction(transaction_id=int(tx_id))
    return {"status": "sent", "transaction_id": tx_id, "response": resp}


@app.post("/api/get_diagnostics")
async def get_diagnostics_api(data: dict):
    """
    API endpoint to send a GetDiagnostics.req to the specified Charge Point.
    """
    cp_id = data.get('charge_point_id')
    location = data.get('location')

    if cp_id not in connected_charge_points:
        raise HTTPException(status_code=404, detail="Charge Point ไม่พบ")

    charge_point = connected_charge_points[cp_id]

    try:
        response = await charge_point.get_diagnostics(location=location)
        return {"status": "success", "response": response}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/remote_start_transaction")
async def remote_start_transaction_api(data: dict):
    """
    API endpoint to send a RemoteStartTransaction.req to the specified Charge Point.
    """
    cp_id = data.get('charge_point_id')
    connector_id = data.get('connector_id')
    id_tag = data.get('id_tag')

    if cp_id not in connected_charge_points:
        raise HTTPException(status_code=404, detail="Charge Point ไม่พบ")

    charge_point = connected_charge_points[cp_id]

    try:
        response = await charge_point.remote_start_transaction(id_tag, connector_id)
        return {"status": "success", "response": response}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/remote_stop_transaction")
async def remote_stop_transaction_api(data: dict):
    """
    API endpoint to send a RemoteStopTransaction.req to the specified Charge Point.
    """
    cp_id = data.get('charge_point_id')
    transaction_id = data.get('transaction_id')

    if cp_id not in connected_charge_points:
        raise HTTPException(status_code=404, detail="Charge Point ไม่พบ")

    charge_point = connected_charge_points[cp_id]

    try:
        response = await charge_point.remote_stop_transaction(transaction_id)
        return {"status": "success", "response": response}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/get_diagnostics")
async def get_diagnostics_api(data: dict):
    """
    API endpoint to send a GetDiagnostics.req to the specified Charge Point.
    """
    cp_id = data.get('charge_point_id')
    location = data.get('location')

    if cp_id not in connected_charge_points:
        raise HTTPException(status_code=404, detail="Charge Point ไม่พบ")

    charge_point = connected_charge_points[cp_id]

    try:
        response = await charge_point.get_diagnostics(location=location)
        return {"status": "success", "response": response}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/change_availability")
async def change_availability_api(data: dict):
    """
    API endpoint to send a ChangeAvailability.req to the specified Charge Point.
    """
    cp_id = data.get('charge_point_id')
    connector_id = data.get('connector_id')
    availability_type = data.get('type')

    if cp_id not in connected_charge_points:
        raise HTTPException(status_code=404, detail="Charge Point ไม่พบ")

    charge_point = connected_charge_points[cp_id]

    if availability_type not in ['Operative', 'Inoperative']:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid availability type: '{availability_type}'. Must be 'Operative' or 'Inoperative'."
        )

    try:
        response = await charge_point.change_availability(connector_id=connector_id, type=availability_type)
        return {"status": "success", "response": response}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/change_configuration")
async def change_configuration_api(data: dict):
    """
    API endpoint to send a ChangeConfiguration.req to the specified Charge Point.
    """
    cp_id = data.get('charge_point_id')
    key = data.get('key')
    value = data.get('value')

    if cp_id not in connected_charge_points:
        raise HTTPException(status_code=404, detail="Charge Point ไม่พบ")

    charge_point = connected_charge_points[cp_id]

    try:
        response = await charge_point.change_configuration(key=key, value=value)
        return {"status": "success", "response": response}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/clear_cache")
async def clear_cache_api(data: dict):
    """
    API endpoint to send a ClearCache.req to the specified Charge Point.
    """
    cp_id = data.get('charge_point_id')

    if cp_id not in connected_charge_points:
        raise HTTPException(status_code=404, detail="Charge Point ไม่พบ")

    charge_point = connected_charge_points[cp_id]

    try:
        response = await charge_point.clear_cache()
        return {"status": "success", "response": response}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/remove_charge_point")
async def remove_charge_point_api(data: dict):
    """
    API endpoint to permanently remove a Charge Point from the system.
    """
    cp_id = data.get('charge_point_id')

    if cp_id in connected_charge_points:
        del connected_charge_points[cp_id]
        logging.info(f"Removed Charge Point ID: {cp_id}")
        await notify_all_dashboard_clients(f"[Remove] Charge Point ID: {cp_id} ถูกลบออกจากระบบแล้ว.")
        return {"status": "success", "message": f"Charge Point {cp_id} ถูกลบแล้ว"}

    raise HTTPException(status_code=404, detail="Charge Point ไม่พบ")


# ------------------ OCPP ChargePoint Handler ------------------
class ChargePoint(BaseChargePoint):
    def __init__(self, id, connection):
        super().__init__(id, connection)
        self.connectors = {}
        self.last_heard = datetime.utcnow()

    async def notify_dashboard(self, message: str):
        for client in list(dashboard_clients):
            try:
                await client.send_text(message)
            except WebSocketDisconnect:
                dashboard_clients.remove(client)

    async def local_start_process(self, id_tag: str, connector_id: int):
        """Simulates the process where a Charge Point initiates a transaction itself."""
        await self.notify_dashboard(f"[{self.id}] จำลองผู้ใช้แสดง idTag '{id_tag}' ที่หัวชาร์จ {connector_id}.")
        auth_response = await self.authorize(id_tag)

        if auth_response.id_tag_info['status'] == AuthorizationStatus.accepted:
            await self.notify_dashboard(f"[{self.id}] การอนุญาตสำเร็จ. จำลองการเสียบสายและเริ่มธุรกรรม.")
            await asyncio.sleep(2)
            transaction_id = int(time.time())
            await self.start_transaction(id_tag=id_tag, connector_id=connector_id, transaction_id=transaction_id)
            return {"status": "Transaction started", "transaction_id": transaction_id}
        else:
            await self.notify_dashboard(f"[{self.id}] การอนุญาตล้มเหลวสำหรับ idTag '{id_tag}'. ธุรกรรมจะไม่เริ่ม.")
            raise Exception("การอนุญาตล้มเหลว")

    async def remote_start_transaction(self, id_tag: str, connector_id: int):
        """Sends a RemoteStartTransaction.req to the Charge Point."""
        request = call.RemoteStartTransactionPayload(
            id_tag=id_tag,
            connector_id=connector_id
        )
        await self.notify_dashboard(f"[RemoteStartTransaction.req] ส่งคำสั่งไปยัง {self.id} เพื่อเริ่มธุรกรรมระยะไกลสำหรับ idTag '{id_tag}' ที่หัวชาร์จ {connector_id}")
        return await self.call(request)

    async def remote_stop_transaction(self, transaction_id: int):
        request = call.RemoteStopTransactionPayload(
            transaction_id=transaction_id
        )
        await self.notify_dashboard(f"[RemoteStopTransaction.req] ส่งคำสั่งไปยัง {self.id} เพื่อหยุดธุรกรรม {transaction_id}")
        return await self.call(request)

    async def get_diagnostics(self, location: str):
        request = call.GetDiagnosticsPayload(
            location=location
        )
        await self.notify_dashboard(f"[GetDiagnostics.req] ส่งคำสั่งไปยัง {self.id} ด้วย location {location}")
        return await self.call(request)

    async def change_availability(self, connector_id: int, type: str):
        """Sends a ChangeAvailability.req to the Charge Point with a validated type."""
        request = call.ChangeAvailabilityPayload(
            connector_id=connector_id,
            type=type
        )
        await self.notify_dashboard(f"[ChangeAvailability.req] ส่งคำสั่งไปยัง {self.id} เพื่อตั้งค่าหัวชาร์จ {connector_id} เป็น '{type}'")
        return await self.call(request)

    async def change_configuration(self, key: str, value: str):
        """Sends a ChangeConfiguration.req to the Charge Point."""
        request = call.ChangeConfigurationPayload(
            key=key,
            value=value
        )
        await self.notify_dashboard(f"[ChangeConfiguration.req] ส่งคำสั่งไปยัง {self.id} เพื่อตั้งค่า '{key}' เป็น '{value}'")
        return await self.call(request)

    async def clear_cache(self):
        """Sends a ClearCache.req to the Charge Point."""
        request = call.ClearCachePayload()
        await self.notify_dashboard(f"[ClearCache.req] ส่งคำสั่งไปยัง {self.id} เพื่อล้างแคช")
        return await self.call(request)

    @on('BootNotification')
    async def on_boot_notification(self, charge_point_model, charge_point_vendor, **kwargs):
        self.connectors[0] = {'status': 'Available', 'power_kw': 0.0, 'last_heard': datetime.utcnow()}
        self.connectors[1] = {'status': 'Available', 'power_kw': 0.0, 'last_heard': datetime.utcnow(), 'transaction_id': None}
        self.connectors[2] = {'status': 'Available', 'power_kw': 0.0, 'last_heard': datetime.utcnow(), 'transaction_id': None}
        self.last_heard = datetime.utcnow()
        msg = f"[BootNotification] {self.id} | Model: {charge_point_model}, Vendor: {charge_point_vendor}. หัวชาร์จ 1 และ 2 พร้อมใช้งาน."
        print(msg)
        await self.notify_dashboard(msg)
        return call_result.BootNotificationPayload(
            current_time=datetime.utcnow().isoformat(),
            interval=10,
            status=RegistrationStatus.accepted
        )

    @on('StatusNotification')
    async def on_status_notification(self, connector_id, error_code, status, **kwargs):
        if connector_id not in self.connectors:
            self.connectors[connector_id] = {'status': status, 'power_kw': 0.0, 'last_heard': datetime.utcnow()}
        else:
            self.connectors[connector_id]['status'] = status
            self.connectors[connector_id]['last_heard'] = datetime.utcnow()

        self.last_heard = datetime.utcnow()
        msg = f"[StatusNotification] {self.id} | หัวชาร์จ: {connector_id}, สถานะ: {status}, Error: {error_code}"
        print(msg)
        await self.notify_dashboard(msg)

        return call_result.StatusNotificationPayload()

    @on('Authorize')
    async def on_authorize(self, id_tag: str):
        status = AuthorizationStatus.accepted if id_tag in AUTHORIZED_ID_TAGS else AuthorizationStatus.invalid
        msg = f"[Authorize] {self.id} | idTag: {id_tag}, สถานะ: {status}"
        print(msg)
        await self.notify_dashboard(msg)
        return call_result.AuthorizePayload(id_tag_info={"status": status})

    @on('StartTransaction')
    async def on_start_transaction(self, connector_id: int, id_tag: str, **kwargs):
        transaction_id = kwargs.get('transaction_id')
        if not transaction_id:
            transaction_id = int(time.time())
   
        if connector_id in self.connectors:
            self.connectors[connector_id]['status'] = 'Charging'
            self.connectors[connector_id]['transaction_id'] = transaction_id
            self.connectors[connector_id]['last_heard'] = datetime.utcnow()

        msg = f"[StartTransaction] {self.id} | หัวชาร์จ: {connector_id}, idTag: {id_tag}, ธุรกรรม ID: {transaction_id}"
        print(msg)
        await self.notify_dashboard(msg)

        # --- Auto-stop tracker: reset/arm on start ---
        try:
            LOW_POWER_TRACK[(self.id, connector_id)] = {"below_since": None}
        except Exception:
            pass

        return call_result.StartTransactionPayload(
            id_tag_info={'status': AuthorizationStatus.accepted},
            transaction_id=transaction_id
        )

    @on('StopTransaction')
    async def on_stop_transaction(self, connector_id: int, transaction_id: int, **kwargs):
        if connector_id in self.connectors:
            self.connectors[connector_id]['status'] = 'Available'
            self.connectors[connector_id]['power_kw'] = 0.0
            self.connectors[connector_id]['transaction_id'] = None
            self.connectors[connector_id]['last_heard'] = datetime.utcnow()

        # --- Auto-stop tracker: clear on stop ---
        try:
            LOW_POWER_TRACK.pop((self.id, connector_id), None)
        except Exception:
            pass

        msg = f"[StopTransaction] {self.id} | หัวชาร์จ: {connector_id}, ธุรกรรม ID: {transaction_id}, สถานะ: เสร็จสิ้น"
        print(msg)
        await self.notify_dashboard(msg)
        return call_result.StopTransactionPayload(
            id_tag_info={'status': AuthorizationStatus.accepted}
        )

    @on('MeterValues')
    async def on_meter_values(self, connector_id: int, meter_value: list, **kwargs):
        power_kw = 0.0
        for mv in meter_value:
            for sv in mv['sampledValue']:
                if sv['measurand'] == Measurand.Power_Active_Import:
                    try:
                        power_kw = float(sv['value']) / 1000.0
                    except (ValueError, KeyError):
                        pass

        if connector_id in self.connectors:
            self.connectors[connector_id]['power_kw'] = power_kw
            self.connectors[connector_id]['last_heard'] = datetime.utcnow()

        self.last_heard = datetime.utcnow()
        msg = f"[MeterValues] {self.id} | หัวชาร์จ: {connector_id}, กำลังไฟ: {power_kw:.2f} kW"
        print(msg)
        await self.notify_dashboard(msg)

        # --- Auto-stop: low power sustained ---
        if AUTO_STOP_CFG.get("enabled", False):
            key = (self.id, connector_id)
            track = LOW_POWER_TRACK.get(key)
            # ต้องมีธุรกรรมกำลังวิ่งอยู่ถึงจะตรวจ auto stop
            tx_id = None
            if connector_id in self.connectors:
                tx_id = self.connectors[connector_id].get("transaction_id")

            if track is not None and tx_id:
                current_kw = self.connectors[connector_id].get("power_kw") or 0.0
                threshold = float(AUTO_STOP_CFG.get("threshold_kw", 0.8))
                window_sec = int(AUTO_STOP_CFG.get("duration_sec", 180))

                now = datetime.utcnow()
                below = current_kw < threshold

                if below:
                    # เริ่มจับเวลาเมื่อเข้าสู่โซนต่ำครั้งแรก
                    if track.get("below_since") is None:
                        track["below_since"] = now
                    else:
                        elapsed = (now - track["below_since"]).total_seconds()
                        if elapsed >= window_sec:
                            # ถึงเวลา auto stop
                            try:
                                await self.notify_dashboard(
                                    f"[AutoStop] {self.id} c{connector_id}: "
                                    f"low power {current_kw:.2f}kW < {threshold:.2f}kW "
                                    f"for {int(elapsed)}s -> RemoteStopTransaction({tx_id})"
                                )
                                await self.remote_stop_transaction(transaction_id=int(tx_id))
                            except Exception as e:
                                await self.notify_dashboard(f"[AutoStop][ERR] remote_stop: {e}")
                            finally:
                                # รีเซ็ต tracker เพื่อไม่ให้ยิงซ้ำหากฝั่ง CP ยังส่งค่าเดิมมา
                                track["below_since"] = None
                else:
                    # กลับมาสูงกว่าเกณฑ์ รีเซ็ตตัวจับเวลา
                    track["below_since"] = None

        return call_result.MeterValuesPayload()

    @on('Heartbeat')
    async def on_heartbeat(self):
        self.last_heard = datetime.utcnow()
        msg = f"[Heartbeat] {self.id} ได้รับแล้ว."
        print(msg)
        await self.notify_dashboard(msg)
        return call_result.HeartbeatPayload(
            current_time=datetime.utcnow().isoformat()
        )

    @on('ChangeAvailability')
    async def on_change_availability(self, connector_id: int, type: str):
        if connector_id in self.connectors:
            self.connectors[connector_id]['status'] = type
            self.connectors[connector_id]['last_heard'] = datetime.utcnow()
            await self.notify_dashboard(f"[ChangeAvailability.conf] {self.id} | สถานะหัวชาร์จ {connector_id} เปลี่ยนเป็น '{type}' ตามคำขอ.")

        # NOTE: OCPP v1.6 expects a specific status, not AuthorizationStatus
        return call_result.ChangeAvailabilityPayload(
            status=ChargePointStatus.accepted
        )

    @on('ClearCache')
    async def on_clear_cache(self):
        await self.notify_dashboard(f"[ClearCache.conf] {self.id} | ยืนยันคำขอล้างแคช.")
        return call_result.ClearCachePayload(
            status=ChargePointStatus.accepted
        )

    @on('RemoteStartTransaction')
    async def on_remote_start_transaction(self, id_tag: str, connector_id: int):
        if id_tag not in AUTHORIZED_ID_TAGS:
            await self.notify_dashboard(f"[RemoteStartTransaction.conf] {self.id} | ปฏิเสธการเริ่มชาร์จระยะไกล: idTag '{id_tag}' ไม่ถูกต้อง.")
            return call_result.RemoteStartTransactionPayload(status=RemoteStartStopStatus.rejected)

        if connector_id not in self.connectors or self.connectors[connector_id]['status'] not in ['Available', 'Operative']:
            await self.notify_dashboard(f"[RemoteStartTransaction.conf] {self.id} | ปฏิเสธการเริ่มชาร์จระยะไกล: หัวชาร์จ {connector_id} ไม่พร้อมใช้งาน.")
            return call_result.RemoteStartTransactionPayload(status=RemoteStartStopStatus.rejected)

        await self.notify_dashboard(f"[RemoteStartTransaction.conf] {self.id} | ยืนยันการเริ่มชาร์จระยะไกลสำหรับ idTag '{id_tag}' ที่หัวชาร์จ {connector_id}.")

        transaction_id = int(time.time())
        await self.start_transaction(id_tag=id_tag, connector_id=connector_id, transaction_id=transaction_id)

        return call_result.RemoteStartTransactionPayload(status=RemoteStartStopStatus.accepted)

    @on('GetConfiguration')
    async def on_get_configuration(self, key: list = None, **kwargs):
        requested_keys = key if key else []
        configuration_key = []
        unknown_key = []

        if not requested_keys:
            requested_keys = list(CONFIGURATION_SETTINGS.keys())

        for k in requested_keys:
            if k in CONFIGURATION_SETTINGS:
                configuration_key.append({
                    "key": k,
                    "value": CONFIGURATION_SETTINGS[k]["value"],
                    "readonly": CONFIGURATION_SETTINGS[k]["readonly"]
                })
            else:
                unknown_key.append(k)

        msg = f"[GetConfiguration] {self.id} | ตอบกลับด้วย {len(configuration_key)} keys ที่รู้จักและ {len(unknown_key)} keys ที่ไม่รู้จัก."
        print(msg)
        await self.notify_dashboard(msg)

        return call_result.GetConfigurationPayload(
            configuration_key=configuration_key,
            unknown_key=unknown_key
        )

    @on('GetDiagnostics')
    async def on_get_diagnostics(self, location, **kwargs):
        file_name = f"diagnostics_{self.id}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.log"
        msg = f"[GetDiagnostics.conf] {self.id} | ยืนยันคำขอ diagnostics. ไฟล์ที่จะอัปโหลด: {file_name}"
        print(msg)
        await self.notify_dashboard(msg)
        return call_result.GetDiagnosticsPayload(file_name=file_name)


# ------------------ OCPP WebSocket Server ------------------
async def on_connect(websocket):
    cp_id = websocket.path.strip("/")
    if not cp_id:
        logging.error("ข้อผิดพลาด: พยายามเชื่อมต่อด้วย Charge Point ID ว่างเปล่า. ปิดการเชื่อมต่อ.")
        await websocket.close()
        return

    # Check if a disconnected CP with this ID is already in the list
    if cp_id in connected_charge_points and connected_charge_points[cp_id].connection is None:
        # It's an offline CP reconnecting. Update its connection.
        charge_point = connected_charge_points[cp_id]
        charge_point.connection = websocket
        logging.info(f"[Reconnected] Charge Point ID: {cp_id}")
        await charge_point.notify_dashboard(f"[Reconnected] Charge Point ID: {cp_id} กลับมาเชื่อมต่อแล้ว.")
        # Re-set connectors to 'Available' or last known state (we'll just use Available for simplicity)
        for conn_id in charge_point.connectors:
            charge_point.connectors[conn_id]['status'] = "Available"
    else:
        # It's a new connection.
        try:
            charge_point = ChargePoint(cp_id, websocket)
            connected_charge_points[cp_id] = charge_point

            logging.info(f"[Connected] Charge Point ID: {cp_id}")
            await charge_point.notify_dashboard(f"[Connected] Charge Point ID: {cp_id} เชื่อมต่อแล้ว.")
        except Exception as e:
            logging.exception(f"[Error] การเชื่อมต่อสำหรับ path '{websocket.path}' ล้มเหลว: {e}")
            return  # Exit function on error

    try:
        await charge_point.start()
    except Exception as e:
        logging.exception(f"[Error] การสื่อสารกับ Charge Point ID '{cp_id}' ล้มเหลว: {e}")
    finally:
        # On disconnect, set the status to 'Offline' instead of removing
        if cp_id in connected_charge_points:
            charge_point_obj = connected_charge_points[cp_id]
            # Disconnect the WebSocket but keep the object in the dictionary
            charge_point_obj.connection = None
            for conn_id in charge_point_obj.connectors:
                charge_point_obj.connectors[conn_id]['status'] = "Offline"
                charge_point_obj.connectors[conn_id]['transaction_id'] = None
                charge_point_obj.connectors[conn_id]['last_heard'] = datetime.utcnow()

            logging.info(f"[Offline] Charge Point ID: {cp_id} is now offline.")
            await charge_point_obj.notify_dashboard(f"[Offline] Charge Point ID: {cp_id} สถานะเปลี่ยนเป็นออฟไลน์.")


async def start_ocpp_server():
    server = await websockets.serve(on_connect, "0.0.0.0", 9000, subprotocols=["ocpp1.6"])
    logging.info("OCPP Server กำลังทำงานที่ ws://0.0.0.0:9000/")
    await server.wait_closed()


# ------------------ Run Everything ------------------
def start_fastapi():
    uvicorn.run(app, host="0.0.0.0", port=8000)

def main():
    threading.Thread(target=start_fastapi, daemon=True).start()

    loop = asyncio.get_event_loop()
    loop.run_until_complete(start_ocpp_server())

if __name__ == '__main__':
    main()