package main

import (
	"bytes"
	"fmt"
	"io/ioutil"
	"net/http"
)

const (
	apiBase = "http://45.136.236.186:8080/api/v1"
	apiKey  = "changeme-123" // ต้องตรงกับ API_KEY ใน central.py
)

func startCharge(cpid string, connectorId int, idTag string) error {
	url := fmt.Sprintf("%s/start", apiBase)
	jsonBody := fmt.Sprintf(`{"cpid":"%s","connectorId":%d,"idTag":"%s"}`, cpid, connectorId, idTag)
	req, err := http.NewRequest("POST", url, bytes.NewBuffer([]byte(jsonBody)))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-API-Key", apiKey)

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	body, _ := ioutil.ReadAll(resp.Body)
	fmt.Println("Start response:", string(body))
	return nil
}

func stopCharge(cpid string, transactionId int) error {
	url := fmt.Sprintf("%s/stop", apiBase)
	jsonBody := fmt.Sprintf(`{"cpid":"%s","transactionId":%d}`, cpid, transactionId)
	req, err := http.NewRequest("POST", url, bytes.NewBuffer([]byte(jsonBody)))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-API-Key", apiKey)

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	body, _ := ioutil.ReadAll(resp.Body)
	fmt.Println("Stop response:", string(body))
	return nil
}

func main() {
	// เริ่มชาร์จ
	err := startCharge("CP_001", 1, "TAG_1234")
	if err != nil {
		fmt.Println("Error starting charge:", err)
	}

	// ตัวอย่างหยุดชาร์จ ใช้ transactionId=3 (ต้องตรงกับที่ได้จาก StartTransaction)
	err = stopCharge("CP_001", 3)
	if err != nil {
		fmt.Println("Error stopping charge:", err)
	}
}
