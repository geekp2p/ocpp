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
	apiBase = "http://45.136.236.186:8080/api/v1"
	apiKey  = "changeme-123"
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
	// บังคับปิดหลังจบ (ซ้ำกับ DisableKeepAlives)
	req.Header.Set("Connection", "close")

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

func startCharge(cpid string, connectorId int, idTag string) error {
	url := fmt.Sprintf("%s/start", apiBase)
	jsonBody := fmt.Sprintf(`{"cpid":"%s","connectorId":%d,"idTag":"%s"}`, cpid, connectorId, idTag)
	return doJSON("POST", url, jsonBody)
}

func stopCharge(cpid string, transactionId int) error {
	url := fmt.Sprintf("%s/stop", apiBase)
	jsonBody := fmt.Sprintf(`{"cpid":"%s","transactionId":%d}`, cpid, transactionId)
	return doJSON("POST", url, jsonBody)
}

func main() {
	if len(os.Args) < 3 {
		fmt.Println("usage:")
		fmt.Println("  go run start_stop.go start <cpid> <connectorId> <idTag>")
		fmt.Println("  go run start_stop.go stop <cpid> <transactionId>")
		return
	}

	cmd := os.Args[1]
	cpid := os.Args[2]

	switch cmd {
	case "start":
		if len(os.Args) < 5 {
			fmt.Println("usage: go run start_stop.go start <cpid> <connectorId> <idTag>")
			return
		}
		connectorId, err := strconv.Atoi(os.Args[3])
		if err != nil {
			fmt.Println("Invalid connectorId:", err)
			return
		}
		idTag := os.Args[4]
		if err := startCharge(cpid, connectorId, idTag); err != nil {
			fmt.Println("Error starting charge:", err)
		}
	case "stop":
		if len(os.Args) < 4 {
			fmt.Println("usage: go run start_stop.go stop <cpid> <transactionId>")
			return
		}
		transactionId, err := strconv.Atoi(os.Args[3])
		if err != nil {
			fmt.Println("Invalid transactionId:", err)
			return
		}
		if err := stopCharge(cpid, transactionId); err != nil {
			fmt.Println("Error stopping charge:", err)
		}
	default:
		fmt.Println("unknown cmd:", cmd)
		fmt.Println("usage:")
		fmt.Println("  go run start_stop.go start <cpid> <connectorId> <idTag>")
		fmt.Println("  go run start_stop.go stop <cpid> <transactionId>")
	}
}
