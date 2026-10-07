package api

import (
	"encoding/json"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestStaticRoutesCoexistWithAPIFallback(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "index.html"), []byte("<!doctype html><title>AniMemo</title>"), 0600); err != nil {
		t.Fatal(err)
	}
	handler := New(nil, Config{PublicOrigin: "http://example.test", WebDir: dir})
	for _, check := range []struct {
		method, path string
		status       int
		html         bool
	}{{"GET", "/", 200, true}, {"GET", "/u/00000000-0000-0000-0000-000000000001", 200, true}, {"GET", "/admin", 200, true}, {"GET", "/api/v1/unknown", 404, false}, {"POST", "/unknown", 405, false}, {"POST", "/api/v1/unknown", 404, false}} {
		req := httptest.NewRequest(check.method, check.path, nil)
		req.Header.Set("Origin", "http://example.test")
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, req)
		if w.Code != check.status || strings.Contains(w.Body.String(), "<!doctype html>") != check.html {
			t.Fatalf("%s %s: %d %s", check.method, check.path, w.Code, w.Body.String())
		}
	}
	req := httptest.NewRequest("GET", "/api/v1/openapi.json", nil)
	w := httptest.NewRecorder()
	handler.ServeHTTP(w, req)
	var contract struct {
		OpenAPI string         `json:"openapi"`
		Paths   map[string]any `json:"paths"`
	}
	if w.Code != 200 || json.Unmarshal(w.Body.Bytes(), &contract) != nil || contract.OpenAPI != "3.1.0" || contract.Paths["/api/v1/auth/two-factor"] == nil {
		t.Fatal("embedded contract is missing or invalid")
	}
}
