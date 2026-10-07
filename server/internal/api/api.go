package api

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"mime"
	"net"
	"net/http"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"animemo.local/server/internal/accounts"
	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/journal"
	"github.com/jackc/pgx/v5/pgxpool"
)

const cookieName = "animemo_session"

type Config struct{ PublicOrigin, WebDir string }
type userKey struct{}

type API struct {
	accounts *accounts.Service
	journal  *journal.Service
	pool     *pgxpool.Pool
	config   Config
	limiter  *authLimiter
}

func New(pool *pgxpool.Pool, config Config) http.Handler {
	a := &API{accounts: accounts.New(pool), journal: journal.New(pool), pool: pool, config: config, limiter: &authLimiter{buckets: map[string]bucket{}}}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/health", func(w http.ResponseWriter, r *http.Request) { write(w, 200, map[string]string{"status": "ok"}) })
	mux.HandleFunc("GET /api/ready", func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
		defer cancel()
		if err := pool.Ping(ctx); err != nil {
			write(w, 503, map[string]string{"status": "unavailable"})
			return
		}
		write(w, 200, map[string]string{"status": "ready"})
	})
	mux.HandleFunc("POST /api/v1/auth/register", a.authLimit(a.register))
	mux.HandleFunc("POST /api/v1/auth/login", a.authLimit(a.login))
	mux.HandleFunc("POST /api/v1/auth/logout", a.logout)
	mux.HandleFunc("GET /api/v1/auth/me", a.require(func(w http.ResponseWriter, r *http.Request) { write(w, 200, currentUser(r)) }))
	mux.HandleFunc("GET /api/v1/entries", a.require(a.list))
	mux.HandleFunc("POST /api/v1/entries", a.require(a.create))
	mux.HandleFunc("GET /api/v1/entries/{id}", a.require(a.get))
	mux.HandleFunc("PATCH /api/v1/entries/{id}", a.require(a.update))
	mux.HandleFunc("DELETE /api/v1/entries/{id}", a.require(a.delete))
	mux.HandleFunc("GET /api/v1/entries/{id}/history", a.require(a.history))
	mux.HandleFunc("POST /api/v1/entries/{id}/history", a.require(a.record))
	mux.HandleFunc("GET /api/v1/history", a.require(a.history))
	mux.HandleFunc("GET /api/v1/stats", a.require(a.stats))
	mux.HandleFunc("GET /api/v1/export", a.require(a.export))
	mux.HandleFunc("/api/", func(w http.ResponseWriter, r *http.Request) { fail(w, fault.New("not_found", "接口不存在。")) })
	if config.WebDir != "" {
		mux.Handle("GET /assets/", http.FileServer(http.Dir(config.WebDir)))
		mux.HandleFunc("GET /favicon.svg", func(w http.ResponseWriter, r *http.Request) {
			http.ServeFile(w, r, filepath.Join(config.WebDir, "favicon.svg"))
		})
		mux.HandleFunc("GET /{$}", func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Cache-Control", "no-cache")
			w.Header().Set("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
			http.ServeFile(w, r, filepath.Join(config.WebDir, "index.html"))
		})
	}
	return a.protect(mux)
}

func currentUser(r *http.Request) accounts.User { return r.Context().Value(userKey{}).(accounts.User) }

func (a *API) protect(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("Referrer-Policy", "same-origin")
		w.Header().Set("X-Frame-Options", "DENY")
		if strings.HasPrefix(r.URL.Path, "/api/") {
			w.Header().Set("Cache-Control", "private, no-store")
		}
		if r.Method != http.MethodGet && r.Method != http.MethodHead && r.Method != http.MethodOptions {
			if r.Header.Get("Origin") != a.config.PublicOrigin {
				fail(w, fault.New("forbidden_origin", "请求来源无效，请从本站页面重新操作。"))
				return
			}
		}
		next.ServeHTTP(w, r)
	})
}

func (a *API) require(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		cookie, err := r.Cookie(cookieName)
		if err != nil {
			fail(w, fault.New("unauthorized", "请先登录。"))
			return
		}
		user, err := a.accounts.Authenticate(r.Context(), cookie.Value)
		if err != nil {
			fail(w, err)
			return
		}
		next(w, r.WithContext(context.WithValue(r.Context(), userKey{}, user)))
	}
}

func (a *API) setSession(w http.ResponseWriter, s accounts.Session) {
	http.SetCookie(w, &http.Cookie{Name: cookieName, Value: s.Token, Path: "/", HttpOnly: true, Secure: strings.HasPrefix(a.config.PublicOrigin, "https://"), SameSite: http.SameSiteLaxMode, Expires: s.ExpiresAt, MaxAge: int(accounts.SessionLifetime.Seconds())})
}

func (a *API) register(w http.ResponseWriter, r *http.Request) {
	var input accounts.Registration
	if !decode(w, r, &input) {
		return
	}
	session, err := a.accounts.Register(r.Context(), input)
	if err != nil {
		fail(w, err)
		return
	}
	a.setSession(w, session)
	write(w, http.StatusCreated, session.User)
}

func (a *API) login(w http.ResponseWriter, r *http.Request) {
	var input accounts.Credentials
	if !decode(w, r, &input) {
		return
	}
	session, err := a.accounts.Login(r.Context(), input)
	if err != nil {
		fail(w, err)
		return
	}
	// Rotating login also revokes the previous browser session, when present.
	if cookie, err := r.Cookie(cookieName); err == nil {
		if err = a.accounts.Logout(r.Context(), cookie.Value); err != nil {
			fail(w, err)
			return
		}
	}
	a.setSession(w, session)
	write(w, 200, session.User)
}

func (a *API) logout(w http.ResponseWriter, r *http.Request) {
	if cookie, err := r.Cookie(cookieName); err == nil {
		if err = a.accounts.Logout(r.Context(), cookie.Value); err != nil {
			fail(w, err)
			return
		}
	}
	http.SetCookie(w, &http.Cookie{Name: cookieName, Value: "", Path: "/", HttpOnly: true, Secure: strings.HasPrefix(a.config.PublicOrigin, "https://"), SameSite: http.SameSiteLaxMode, MaxAge: -1, Expires: time.Unix(0, 0)})
	w.WriteHeader(http.StatusNoContent)
}

func (a *API) list(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	page, size := 1, 12
	var err error
	if q.Has("page") {
		page, err = strconv.Atoi(q.Get("page"))
		if err != nil {
			fail(w, fault.Field("page", "页码无效。"))
			return
		}
	}
	if q.Has("page_size") {
		size, err = strconv.Atoi(q.Get("page_size"))
		if err != nil {
			fail(w, fault.Field("page_size", "每页数量无效。"))
			return
		}
	}
	out, err := a.journal.List(r.Context(), currentUser(r).ID, journal.Filter{Search: q.Get("search"), Status: q.Get("status"), Sort: q.Get("sort"), Page: page, PageSize: size})
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, out)
}

func (a *API) create(w http.ResponseWriter, r *http.Request) {
	var input journal.Create
	if !decode(w, r, &input) {
		return
	}
	e, err := a.journal.Create(r.Context(), currentUser(r).ID, input)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 201, e)
}

func (a *API) get(w http.ResponseWriter, r *http.Request) {
	e, err := a.journal.Get(r.Context(), currentUser(r).ID, r.PathValue("id"))
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, e)
}

func (a *API) update(w http.ResponseWriter, r *http.Request) {
	var input journal.Patch
	if !decode(w, r, &input) {
		return
	}
	e, err := a.journal.Update(r.Context(), currentUser(r).ID, r.PathValue("id"), input)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, e)
}

func (a *API) delete(w http.ResponseWriter, r *http.Request) {
	version, err := strconv.Atoi(r.URL.Query().Get("version"))
	if err != nil {
		fail(w, fault.Field("version", "缺少记录版本。"))
		return
	}
	if err = a.journal.Delete(r.Context(), currentUser(r).ID, r.PathValue("id"), version); err != nil {
		fail(w, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func (a *API) history(w http.ResponseWriter, r *http.Request) {
	items, err := a.journal.History(r.Context(), currentUser(r).ID, r.PathValue("id"))
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, map[string]any{"items": items})
}

func (a *API) record(w http.ResponseWriter, r *http.Request) {
	var input journal.RecordInput
	if !decode(w, r, &input) {
		return
	}
	out, err := a.journal.RecordWatch(r.Context(), currentUser(r).ID, r.PathValue("id"), input)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 201, out)
}

func (a *API) stats(w http.ResponseWriter, r *http.Request) {
	stats, err := a.journal.Stats(r.Context(), currentUser(r).ID)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, stats)
}

func (a *API) export(w http.ResponseWriter, r *http.Request) {
	out, err := a.journal.Export(r.Context(), currentUser(r).ID)
	if err != nil {
		fail(w, err)
		return
	}
	w.Header().Set("Content-Disposition", `attachment; filename="animemo-journal-`+time.Now().UTC().Format("2006-01-02")+`.json"`)
	write(w, 200, out)
}

func decode(w http.ResponseWriter, r *http.Request, target any) bool {
	media, _, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
	if err != nil || media != "application/json" {
		fail(w, fault.New("unsupported_media_type", "请发送 JSON 请求。"))
		return false
	}
	r.Body = http.MaxBytesReader(w, r.Body, 64<<10)
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	if err = decoder.Decode(target); err != nil {
		fail(w, fault.New("invalid_json", "请求内容无效，或包含未知字段。"))
		return false
	}
	if err = decoder.Decode(&struct{}{}); err != io.EOF {
		fail(w, fault.New("invalid_json", "请求必须只包含一个 JSON 对象。"))
		return false
	}
	return true
}

func write(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(value); err != nil {
		slog.Warn("response write failed", "error", err)
	}
}

func fail(w http.ResponseWriter, err error) {
	if errors.Is(err, context.Canceled) {
		return
	}
	var problem *fault.Error
	if !errors.As(err, &problem) {
		slog.Error("request failed", "error_type", strings.SplitN(err.Error(), ":", 2)[0])
		problem = fault.New("internal_error", "暂时无法完成操作，请稍后重试。")
	}
	status := map[string]int{"validation_error": 400, "invalid_json": 400, "unauthorized": 401, "invalid_credentials": 401, "forbidden_origin": 403, "not_found": 404, "version_conflict": 409, "email_taken": 409, "idempotency_conflict": 409, "export_too_large": 413, "unsupported_media_type": 415, "rate_limited": 429, "internal_error": 500}[problem.Code]
	if status == 0 {
		status = 500
	}
	write(w, status, map[string]any{"error": problem})
}

type bucket struct {
	attempts int
	expires  time.Time
}
type authLimiter struct {
	sync.Mutex
	buckets map[string]bucket
}

func (a *API) authLimit(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		ip, _, err := net.SplitHostPort(r.RemoteAddr)
		if err != nil {
			ip = r.RemoteAddr
		}
		now := time.Now()
		a.limiter.Lock()
		for key, b := range a.limiter.buckets {
			if !b.expires.After(now) {
				delete(a.limiter.buckets, key)
			}
		}
		b, exists := a.limiter.buckets[ip]
		if !exists {
			b = bucket{expires: now.Add(15 * time.Minute)}
		}
		// ponytail: process-local abuse limit; shared limit needed if API replicas are added.
		allowed := b.attempts < 20 && (exists || len(a.limiter.buckets) < 4096)
		if allowed {
			b.attempts++
			a.limiter.buckets[ip] = b
		}
		a.limiter.Unlock()
		if !allowed {
			w.Header().Set("Retry-After", "900")
			fail(w, fault.New("rate_limited", "尝试次数较多，请 15 分钟后再试。"))
			return
		}
		next(w, r)
	}
}
