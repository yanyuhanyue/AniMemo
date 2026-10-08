package api

import (
	"animemo.local/server/internal/id"
	"animemo.local/server/internal/jobs"
	"animemo.local/server/internal/telemetry"
	"log/slog"
	"net/http"
	"time"
)

func observe(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requestID := id.New()
		w.Header().Set("X-Request-ID", requestID)
		started := time.Now()
		// Never log query strings, cookies, filenames or user-controlled paths.
		defer func() {
			slog.Info("http.request", "request_id", requestID, "method", r.Method, "duration_ms", time.Since(started).Milliseconds())
		}()
		next.ServeHTTP(w, r.WithContext(telemetry.WithRequestID(r.Context(), requestID)))
	})
}
func (a *API) runtimeStatus(w http.ResponseWriter, r *http.Request) {
	out, err := jobs.New(a.pool).Status(r.Context())
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, out)
}
