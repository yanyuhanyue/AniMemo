package main

import (
	"encoding/json"
	"net"
	"os"
	"strings"
	"time"

	"animemo.local/server/pkg/pluginproto"
)

func main() {
	var in pluginproto.Request
	_ = json.NewDecoder(os.Stdin).Decode(&in)
	switch in.Text {
	case "loop":
		for {
		}
	case "output":
		for {
			_, _ = os.Stdout.WriteString(strings.Repeat("x", 65536))
		}
	case "memory":
		data := make([]byte, 256<<20)
		for i := range data {
			data[i] = byte(i)
		}
		os.Stdout.Write(data)
	case "trap":
		panic("private-file-content-must-not-leak")
	case "inspect":
		if len(os.Environ()) != 0 {
			panic("inherited environment")
		}
		if _, err := os.ReadFile("/etc/passwd"); err == nil {
			panic("inherited filesystem")
		}
		if conn, err := net.DialTimeout("tcp", "127.0.0.1:18081", time.Millisecond); err == nil {
			conn.Close()
			panic("network escape")
		}
	case "protocol":
		json.NewEncoder(os.Stdout).Encode(pluginproto.Response{Protocol: 2, Format: "csv", Data: "title\nunexpected\n"})
		return
	case "unknown":
		os.Stdout.WriteString(`{"protocol":1,"format":"csv","data":"title\nbad\n","capability":"write"}`)
		return
	}
	json.NewEncoder(os.Stdout).Encode(pluginproto.Response{Protocol: 1, Format: "csv", Data: "title\nisolated\n"})
}
