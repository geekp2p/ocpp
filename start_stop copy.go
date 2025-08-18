package main

import (
	"bytes"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"strconv"
	"time"
)

const (
	apiBase      = "http://45.136.236.186:8080"
	apiKey       = "changeme-123"
	defaultIdTag = "DEMO_IDTAG"
)

var httpClient = &http.Client{
	Timeout: 15 * time.Second,
	Transport: &http.Transport{
		DisableKeepAlives: true,
		DialContext: (&net.Dialer{
			Timeout:   5 * time.Second,
			KeepAlive: 0,
		}).DialContext,
		TLSHandshakeTimeout:   5 * time.Second,
		ExpectContinueTimeout: 0,
		IdleConnTimeout:       0,
		MaxIdleConns:          0,
	},
}

func doJSON(method, url, body string) error {
	req, err := http.NewRequest(method, url, bytes.NewBufferString(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-API-Key", apiKey)
	req.Header.Set("Connection", "close") // ปิดหลังจบ (ซ้ำกับ DisableKeepAlives)

	resp, err := httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	b, _ := io.ReadAll(resp.Body)
	fmt.Printf("%s %s -> %d %s\n", method, url, resp.StatusCode, http.StatusText(resp.StatusCode))
	fmt.Println(string(b))
	return nil
}

func startCharge(cpid string, connectorId int, idTag string, txId *int) error {
	url := fmt.Sprintf("%s/api/v1/start", apiBase)
	jsonBody := fmt.Sprintf(`{"cpid":"%s","connectorId":%d,"idTag":"%s"`, cpid, connectorId, idTag)
	if txId != nil {
		jsonBody += fmt.Sprintf(`,"transactionId":%d`, *txId)
	}
	jsonBody += "}"
	return doJSON("POST", url, jsonBody)
}

func stopCharge(cpid string, connectorId int, idTag *string, txId *int) error {
	if txId == nil && idTag == nil {
		url := fmt.Sprintf("%s/charge/stop", apiBase)
		jsonBody := fmt.Sprintf(`{"cpid":"%s","connectorId":%d}`, cpid, connectorId)
		return doJSON("POST", url, jsonBody)
	}
	url := fmt.Sprintf("%s/api/v1/stop", apiBase)
	jsonBody := fmt.Sprintf(`{"cpid":"%s","connectorId":%d`, cpid, connectorId)
	if idTag != nil {
		jsonBody += fmt.Sprintf(`,"idTag":"%s"`, *idTag)
	}
	if txId != nil {
		jsonBody += fmt.Sprintf(`,"transactionId":%d`, *txId)
	}
	jsonBody += "}"
	return doJSON("POST", url, jsonBody)
}

func startByVID(vid string) error {
	url := fmt.Sprintf("%s/api/v1/start_vid", apiBase)
	jsonBody := fmt.Sprintf(`{"vid":"%s"}`, vid)
	return doJSON("POST", url, jsonBody)
}

func stopByVID(vid string) error {
	url := fmt.Sprintf("%s/api/v1/stop_vid", apiBase)
	jsonBody := fmt.Sprintf(`{"vid":"%s"}`, vid)
	return doJSON("POST", url, jsonBody)
}

func main() {
	if len(os.Args) < 3 {
		fmt.Println("usage:")
		fmt.Println("  go run start_stop.go start <cpid> <connectorId> [idTag] [transactionId]")
		fmt.Println("  go run start_stop.go stop  <cpid> <connectorId> [idTag] [transactionId]")
		fmt.Println("  go run start_stop.go start <vid>")
		fmt.Println("  go run start_stop.go stop  <vid>")
		fmt.Println("      If idTag/transactionId are omitted, defaults are used and /charge/stop may be called")
		return
	}

	cmd := os.Args[1]

	switch cmd {
	case "start":
		if len(os.Args) == 3 {
			vid := os.Args[2]
			if err := startByVID(vid); err != nil {
				fmt.Println("Error starting by vid:", err)
			}
			return
		}
		if len(os.Args) < 4 {
			fmt.Println("usage: go run start_stop.go start <cpid> <connectorId> [idTag] [transactionId]")
			return
		}
		cpid := os.Args[2]
		connectorId, err := strconv.Atoi(os.Args[3])
		if err != nil {
			fmt.Println("Invalid connectorId:", err)
			return
		}
		idTag := defaultIdTag
		var txId *int
		if len(os.Args) >= 5 {
			idTag = os.Args[4]
		}
		if len(os.Args) >= 6 {
			t, err := strconv.Atoi(os.Args[5])
			if err != nil {
				fmt.Println("Invalid transactionId:", err)
				return
			}
			txId = &t
		}
		if err := startCharge(cpid, connectorId, idTag, txId); err != nil {
			fmt.Println("Error starting charge:", err)
		}
	case "stop":
		if len(os.Args) == 3 {
			vid := os.Args[2]
			if err := stopByVID(vid); err != nil {
				fmt.Println("Error stopping by vid:", err)
			}
			return
		}
		if len(os.Args) < 4 {
			fmt.Println("usage: go run start_stop.go stop <cpid> <connectorId> [idTag] [transactionId]")
			fmt.Println("       omit idTag/transactionId to call /charge/stop")
			return
		}
		cpid := os.Args[2]
		connectorId, err := strconv.Atoi(os.Args[3])
		if err != nil {
			fmt.Println("Invalid connectorId:", err)
			return
		}
		var idTag *string
		var txId *int
		if len(os.Args) >= 5 {
			s := os.Args[4]
			idTag = &s
		}
		if len(os.Args) >= 6 {
			t, err := strconv.Atoi(os.Args[5])
			if err != nil {
				fmt.Println("Invalid transactionId:", err)
				return
			}
			txId = &t
		}
		if err := stopCharge(cpid, connectorId, idTag, txId); err != nil {
			fmt.Println("Error stopping charge:", err)
		}
	default:
		fmt.Println("unknown cmd:", cmd)
		fmt.Println("usage:")
		fmt.Println("  go run start_stop.go start <cpid> <connectorId> [idTag] [transactionId]")
		fmt.Println("  go run start_stop.go stop  <cpid> <connectorId> [idTag] [transactionId]")
		fmt.Println("  go run start_stop.go start <vid>")
		fmt.Println("  go run start_stop.go stop  <vid>")
	}
}
