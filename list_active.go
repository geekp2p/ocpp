package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
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
		DisableKeepAlives:     true,
		DialContext:           (&net.Dialer{Timeout: 5 * time.Second, KeepAlive: 0}).DialContext,
		TLSHandshakeTimeout:   5 * time.Second,
		ExpectContinueTimeout: 0,
		IdleConnTimeout:       0,
		MaxIdleConns:          0,
	},
}

type session struct {
	CPID          string `json:"cpid"`
	ConnectorID   int    `json:"connectorId"`
	IDTag         string `json:"idTag"`
	TransactionID int    `json:"transactionId"`
}

func doJSON(method, url, body string) (int, []byte, error) {
	req, err := http.NewRequest(method, url, bytes.NewBufferString(body))
	if err != nil {
		return 0, nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-API-Key", apiKey)
	req.Header.Set("Connection", "close")
	resp, err := httpClient.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	fmt.Printf("%s %s -> %d %s\n", method, url, resp.StatusCode, http.StatusText(resp.StatusCode))
	fmt.Println(string(b))
	return resp.StatusCode, b, nil
}

func computeHash(cpid string, connectorId int, idTag, txId, ts string) string {
	canonical := fmt.Sprintf("%s|%d|%s|%s|%s|-|-", cpid, connectorId, idTag, txId, ts)
	sum := sha256.Sum256([]byte(canonical))
	return fmt.Sprintf("%x", sum)
}

func startCharge(cpid string, connectorId int, idTag string, txId *int) error {
	url := fmt.Sprintf("%s/api/v1/start", apiBase)
	ts := time.Now().UTC().Format(time.RFC3339)
	txStr := "-"
	if txId != nil {
		txStr = strconv.Itoa(*txId)
	}
	hash := computeHash(cpid, connectorId, idTag, txStr, ts)
	jsonBody := fmt.Sprintf(`{"cpid":"%s","connectorId":%d,"idTag":"%s","timestamp":"%s","hash":"%s"`, cpid, connectorId, idTag, ts, hash)
	if txId != nil {
		jsonBody += fmt.Sprintf(`,"transactionId":%d`, *txId)
	}
	jsonBody += "}"
	_, _, err := doJSON("POST", url, jsonBody)
	return err
}

func stopCharge(cpid string, connectorId int, idTag *string, txId *int) error {
	url := fmt.Sprintf("%s/api/v1/stop", apiBase)
	ts := time.Now().UTC().Format(time.RFC3339)
	id := "-"
	if idTag != nil {
		id = *idTag
	}
	txStr := "-"
	if txId != nil {
		txStr = strconv.Itoa(*txId)
	}
	hash := computeHash(cpid, connectorId, id, txStr, ts)
	jsonBody := fmt.Sprintf(`{"cpid":"%s","connectorId":%d,"timestamp":"%s","hash":"%s"`, cpid, connectorId, ts, hash)
	if idTag != nil {
		jsonBody += fmt.Sprintf(`,"idTag":"%s"`, *idTag)
	}
	if txId != nil {
		jsonBody += fmt.Sprintf(`,"transactionId":%d`, *txId)
	}
	jsonBody += "}"
	status, _, err := doJSON("POST", url, jsonBody)
	if err != nil {
		return err
	}
	if status == http.StatusNotFound && txId == nil && idTag == nil {
		relURL := fmt.Sprintf("%s/api/v1/release", apiBase)
		relBody := fmt.Sprintf(`{"cpid":"%s","connectorId":%d}`, cpid, connectorId)
		_, _, err = doJSON("POST", relURL, relBody)
	}
	return err
}

func main() {
	if len(os.Args) >= 2 {
		cmd := os.Args[1]
		switch cmd {
		case "start":
			if len(os.Args) < 4 {
				fmt.Println("usage: go run list_active.go start <cpid> <connectorId> [idTag] [transactionId]")
				return
			}
			cpid := os.Args[2]
			connectorId, err := strconv.Atoi(os.Args[3])
			if err != nil {
				fmt.Println("invalid connectorId:", err)
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
					fmt.Println("invalid transactionId:", err)
					return
				}
				txId = &t
			}
			if err := startCharge(cpid, connectorId, idTag, txId); err != nil {
				fmt.Println("start error:", err)
			}
			return
		case "stop":
			if len(os.Args) < 4 {
				fmt.Println("usage: go run list_active.go stop <cpid> <connectorId> [idTag] [transactionId]")
				return
			}
			cpid := os.Args[2]
			connectorId, err := strconv.Atoi(os.Args[3])
			if err != nil {
				fmt.Println("invalid connectorId:", err)
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
					fmt.Println("invalid transactionId:", err)
					return
				}
				txId = &t
			}
			if err := stopCharge(cpid, connectorId, idTag, txId); err != nil {
				fmt.Println("stop error:", err)
			}
			return
		}
	}

	url := fmt.Sprintf("%s/api/v1/active", apiBase)
	req, err := http.NewRequest("GET", url, nil)
	if err != nil {
		fmt.Println("build request:", err)
		return
	}
	req.Header.Set("X-API-Key", apiKey)
	req.Header.Set("Connection", "close")
	resp, err := httpClient.Do(req)
	if err != nil {
		fmt.Println("http:", err)
		return
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK {
		fmt.Printf("GET %s -> %d %s\n", url, resp.StatusCode, resp.Status)
		fmt.Println(string(body))
		return
	}
	var out struct {
		Sessions []session `json:"sessions"`
	}
	if err := json.Unmarshal(body, &out); err != nil {
		fmt.Println("parse:", err)
		fmt.Println(string(body))
		return
	}
	if len(out.Sessions) == 0 {
		fmt.Println("no active sessions")
		return
	}
	for _, s := range out.Sessions {
		transactionID := s.TransactionID
		fmt.Printf("%s %d %s %d\n", s.CPID, s.ConnectorID, s.IDTag, transactionID)
	}
}
