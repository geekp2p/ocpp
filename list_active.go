package main

import (
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"time"
)

const (
	apiBase = "http://45.136.236.186:8080"
	apiKey  = "changeme-123"
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

func main() {
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
