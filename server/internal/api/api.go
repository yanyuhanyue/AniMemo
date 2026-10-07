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
	"animemo.local/server/internal/apicontract"
	"animemo.local/server/internal/bangumi"
	"animemo.local/server/internal/external"
	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/journal"
	"animemo.local/server/internal/mailer"
	"animemo.local/server/internal/mediastore"
	"animemo.local/server/internal/plugins"
	"github.com/jackc/pgx/v5/pgxpool"
)

const cookieName = "animemo_session"

type Config struct {
	Storage                          *mediastore.Store
	OAuth                            bangumi.OAuthConfig
	External                         *external.Service
	Mail                             *mailer.Client
	PublicOrigin, WebDir, SetupToken string
	SecretKey                        []byte
	Bangumi                          *bangumi.Client
}
type userKey struct{}

type API struct {
	storage       *mediastore.Store
	accounts      *accounts.Service
	journal       *journal.Service
	plugins       *plugins.Service
	external      *external.Service
	pool          *pgxpool.Pool
	config        Config
	limiter       *authLimiter
	coverSlots    chan struct{}
	transferSlots chan struct{}
}

func New(pool *pgxpool.Pool, config Config) http.Handler {
	a := &API{accounts: accounts.New(pool), journal: journal.New(pool), plugins: plugins.New(pool), pool: pool, config: config, limiter: &authLimiter{buckets: map[string]bucket{}}, coverSlots: make(chan struct{}, 2), transferSlots: make(chan struct{}, 1)}
	if err := a.accounts.ConfigureEncryption(config.SecretKey); err != nil {
		panic(err)
	}
	if err := a.accounts.ConfigureMail(config.Mail, config.PublicOrigin); err != nil {
		panic(err)
	}
	a.storage = config.Storage
	if a.storage == nil {
		a.storage = mediastore.New(pool, nil)
	}
	a.accounts.ConfigureStorage(a.storage)
	a.journal.ConfigureStorage(a.storage)
	a.external = config.External
	if a.external == nil {
		a.external = external.New(pool, config.Bangumi)
		if err := a.external.ConfigureOAuth(config.OAuth, config.SecretKey, config.PublicOrigin); err != nil {
			panic(err)
		}
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/v1/openapi.json", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.Write(apicontract.Document)
	})
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
	mux.HandleFunc("GET /api/v1/setup", a.setupStatus)
	mux.HandleFunc("POST /api/v1/setup", a.authLimit(a.setup))
	mux.HandleFunc("GET /api/v1/site", a.site)
	mux.HandleFunc("PUT /api/v1/admin/site", a.admin(a.updateSite))
	mux.HandleFunc("GET /api/v1/admin/users", a.admin(a.adminUsers))
	mux.HandleFunc("POST /api/v1/admin/users/{id}", a.admin(a.adminUserAction))
	mux.HandleFunc("GET /api/v1/presets", a.require(a.presets))
	mux.HandleFunc("PUT /api/v1/admin/presets", a.admin(a.savePreset))
	mux.HandleFunc("DELETE /api/v1/admin/presets", a.admin(a.savePreset))
	mux.HandleFunc("GET /api/v1/admin/status", a.admin(a.instanceStatus))
	mux.HandleFunc("GET /api/v1/admin/media/storage", a.admin(a.storageStatus))
	mux.HandleFunc("PUT /api/v1/admin/media/storage", a.admin(a.setStorage))
	mux.HandleFunc("POST /api/v1/admin/media/storage/probe", a.admin(a.probeStorage))
	mux.HandleFunc("GET /api/v1/admin/media/migrations/{id}", a.admin(a.storageManifest))
	mux.HandleFunc("POST /api/v1/admin/maintenance", a.admin(a.maintenance))
	mux.HandleFunc("GET /api/v1/admin/audit", a.admin(a.audit))
	mux.HandleFunc("GET /api/v1/admin/plugins", a.admin(a.pluginList))
	mux.HandleFunc("POST /api/v1/admin/plugins", a.admin(a.pluginInstall))
	mux.HandleFunc("POST /api/v1/admin/plugins/{slug}", a.admin(a.pluginChange))
	mux.HandleFunc("GET /api/v1/plugins", a.require(a.pluginList))
	mux.HandleFunc("POST /api/v1/plugins/{slug}/imports", a.require(a.pluginImport))
	mux.HandleFunc("POST /api/v1/auth/register", a.authLimit(a.register))
	mux.HandleFunc("GET /api/v1/auth/options", a.emailOptions)
	mux.HandleFunc("POST /api/v1/auth/email/request", a.authLimit(a.requestEmail))
	mux.HandleFunc("POST /api/v1/auth/email/verify", a.authLimit(a.confirmEmail))
	mux.HandleFunc("POST /api/v1/auth/password/reset", a.authLimit(a.confirmEmail))
	mux.HandleFunc("POST /api/v1/auth/login", a.authLimit(a.login))
	mux.HandleFunc("POST /api/v1/auth/logout", a.logout)
	mux.HandleFunc("GET /api/v1/auth/me", a.require(func(w http.ResponseWriter, r *http.Request) { write(w, 200, currentUser(r)) }))
	mux.HandleFunc("PUT /api/v1/settings", a.require(a.settings))
	mux.HandleFunc("POST /api/v1/settings/publication", a.require(a.publication))
	mux.HandleFunc("POST /api/v1/auth/two-factor/begin", a.require(a.authLimit(a.beginTwoFactor)))
	mux.HandleFunc("POST /api/v1/auth/two-factor", a.require(a.authLimit(a.changeTwoFactor)))
	mux.HandleFunc("POST /api/v1/auth/password", a.require(a.authLimit(a.password)))
	mux.HandleFunc("POST /api/v1/auth/logout-all", a.require(a.logoutAll))
	mux.HandleFunc("DELETE /api/v1/auth/account", a.require(a.authLimit(a.deleteAccount)))
	mux.HandleFunc("GET /api/v1/avatar/{revision}", a.require(a.avatar))
	mux.HandleFunc("PUT /api/v1/avatar", a.require(a.setAvatar))
	mux.HandleFunc("DELETE /api/v1/avatar", a.require(a.deleteAvatar))
	mux.HandleFunc("GET /api/v1/entries", a.require(a.list))
	mux.HandleFunc("GET /api/v1/providers/bangumi/subjects", a.require(a.searchSubjects))
	mux.HandleFunc("GET /api/v1/connections/bangumi", a.require(a.bangumiConnection))
	mux.HandleFunc("POST /api/v1/connections/bangumi/authorize", a.require(a.authLimit(a.beginBangumi)))
	mux.HandleFunc("DELETE /api/v1/connections/bangumi", a.require(a.disconnectBangumi))
	mux.HandleFunc("POST /api/v1/connections/bangumi/verify", a.require(a.authLimit(a.verifyBangumi)))
	mux.HandleFunc("GET /api/v1/connections/bangumi/callback", a.bangumiCallback)
	mux.HandleFunc("GET /api/v1/connections/bangumi/sync", a.require(a.syncJobs))
	mux.HandleFunc("POST /api/v1/connections/bangumi/sync", a.require(a.startSync))
	mux.HandleFunc("GET /api/v1/connections/bangumi/sync/{id}", a.require(a.syncJob))
	mux.HandleFunc("POST /api/v1/connections/bangumi/sync/{id}", a.require(a.changeSync))
	mux.HandleFunc("GET /api/v1/providers/bangumi/subjects/{subject}", a.require(a.subject))
	mux.HandleFunc("GET /api/v1/providers/bangumi/subjects/{subject}/cover", a.require(a.subjectCover))
	mux.HandleFunc("POST /api/v1/entries/from-bangumi", a.require(a.applySource))
	mux.HandleFunc("POST /api/v1/entries/{id}/source", a.require(a.applySource))
	mux.HandleFunc("DELETE /api/v1/entries/{id}/source", a.require(a.unbindSource))
	mux.HandleFunc("POST /api/v1/entries", a.require(a.create))
	mux.HandleFunc("GET /api/v1/entries/{id}", a.require(a.get))
	mux.HandleFunc("PATCH /api/v1/entries/{id}", a.require(a.update))
	mux.HandleFunc("DELETE /api/v1/entries/{id}", a.require(a.delete))
	mux.HandleFunc("GET /api/v1/entries/{id}/cover/{revision}", a.require(a.cover))
	mux.HandleFunc("PUT /api/v1/entries/{id}/cover", a.require(a.setCover))
	mux.HandleFunc("DELETE /api/v1/entries/{id}/cover", a.require(a.deleteCover))
	mux.HandleFunc("GET /api/v1/entries/{id}/history", a.require(a.history))
	mux.HandleFunc("POST /api/v1/entries/{id}/history", a.require(a.record))
	mux.HandleFunc("GET /api/v1/history", a.require(a.history))
	mux.HandleFunc("GET /api/v1/history/page", a.require(a.historyPage))
	mux.HandleFunc("PATCH /api/v1/entries/{id}/history/{record}", a.require(a.updateRecord))
	mux.HandleFunc("DELETE /api/v1/entries/{id}/history/{record}", a.require(a.deleteRecord))
	mux.HandleFunc("POST /api/v1/entries/bulk", a.require(a.bulk))
	mux.HandleFunc("GET /api/v1/tags", a.require(a.tags))
	mux.HandleFunc("PUT /api/v1/tags", a.require(a.setTag))
	mux.HandleFunc("GET /api/v1/filters", a.require(a.filters))
	mux.HandleFunc("POST /api/v1/filters", a.require(a.saveFilter))
	mux.HandleFunc("DELETE /api/v1/filters/{id}", a.require(a.deleteFilter))
	mux.HandleFunc("GET /api/v1/analytics", a.require(a.analytics))
	mux.HandleFunc("GET /api/v1/stats", a.require(a.stats))
	mux.HandleFunc("GET /api/v1/export", a.require(a.export))
	mux.HandleFunc("GET /api/v1/backup", a.require(a.backup))
	mux.HandleFunc("GET /api/v1/imports", a.require(a.imports))
	mux.HandleFunc("POST /api/v1/imports", a.require(a.newImport))
	mux.HandleFunc("GET /api/v1/imports/{id}", a.require(a.getImport))
	mux.HandleFunc("POST /api/v1/imports/{id}", a.require(a.importAction))
	mux.HandleFunc("GET /api/v1/columns", a.require(a.columns))
	mux.HandleFunc("POST /api/v1/columns", a.require(a.saveColumn))
	mux.HandleFunc("GET /api/v1/columns/{id}", a.require(a.column))
	mux.HandleFunc("PUT /api/v1/columns/{id}", a.require(a.saveColumn))
	mux.HandleFunc("POST /api/v1/columns/{id}", a.require(a.columnAction))
	mux.HandleFunc("PUT /api/v1/columns/{id}/cover", a.require(a.setColumnCover))
	mux.HandleFunc("DELETE /api/v1/columns/{id}/cover", a.require(a.setColumnCover))
	mux.HandleFunc("GET /api/v1/columns/{id}/cover/{revision}", a.require(a.columnCover))
	mux.HandleFunc("GET /api/v1/public/columns", a.publicColumns)
	mux.HandleFunc("GET /api/v1/public/columns/{id}", a.publicColumn)
	mux.HandleFunc("GET /api/v1/public/columns/{id}/cover/{revision}", a.publicColumnCover)
	mux.HandleFunc("GET /api/v1/admin/resources", a.admin(a.resources))
	mux.HandleFunc("GET /api/v1/admin/resources/{kind}/{id}", a.admin(a.resource))
	mux.HandleFunc("POST /api/v1/admin/resources/{kind}/{id}", a.admin(a.resourceAction))
	mux.HandleFunc("POST /api/v1/admin/columns/{id}", a.admin(a.moderateColumn))
	mux.HandleFunc("GET /api/v1/public/showcases", a.directory)
	mux.HandleFunc("GET /api/v1/public/showcases/{slug}", a.publicEntries)
	mux.HandleFunc("GET /api/v1/public/showcases/{slug}/avatar/{revision}", a.publicAvatar)
	mux.HandleFunc("GET /api/v1/public/catalog", a.publicEntries)
	mux.HandleFunc("GET /api/v1/public/shared/{slug}", a.shared)
	mux.HandleFunc("GET /api/v1/public/shared/{slug}/cover/{revision}", a.publicCover)
	mux.HandleFunc("POST /api/v1/entries/{id}/share/reset", a.require(a.resetShare))
	mux.HandleFunc("/api/", func(w http.ResponseWriter, r *http.Request) { fail(w, fault.New("not_found", "接口不存在。")) })
	if config.WebDir != "" {
		mux.Handle("GET /assets/", http.FileServer(http.Dir(config.WebDir)))
		mux.HandleFunc("GET /favicon.svg", func(w http.ResponseWriter, r *http.Request) {
			http.ServeFile(w, r, filepath.Join(config.WebDir, "favicon.svg"))
		})
		mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
			if r.Method != http.MethodGet && r.Method != http.MethodHead {
				w.Header().Set("Allow", "GET, HEAD")
				w.WriteHeader(http.StatusMethodNotAllowed)
				return
			}
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
		if user.EmailVerificationRequired && !user.EmailVerified {
			fail(w, fault.New("email_verification_required", "请先完成邮箱验证。"))
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
	if a.accounts.MailEnabled() {
		write(w, 202, map[string]string{"message": "如果这个邮箱可用于注册，验证邮件会稍后送达；已有账号请直接登录或找回密码。"})
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
	select {
	case a.transferSlots <- struct{}{}:
		defer func() { <-a.transferSlots }()
	default:
		w.Header().Set("Retry-After", "2")
		fail(w, fault.New("media_busy", "正在处理其他数据文件，请稍后重试。"))
		return
	}
	out, err := a.journal.Export(r.Context(), currentUser(r).ID)
	if err != nil {
		fail(w, err)
		return
	}
	data, err := json.Marshal(out)
	if err != nil {
		fail(w, err)
		return
	}
	if len(data) > journal.MaxJournalBytes {
		fail(w, fault.New("export_too_large", "文字导出超过 64 MiB，请使用实例备份。"))
		return
	}
	w.Header().Set("Content-Disposition", `attachment; filename="animemo-journal-`+time.Now().UTC().Format("2006-01-02")+`.json"`)
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Write(data)
}

func decode(w http.ResponseWriter, r *http.Request, target any) bool {
	media, _, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
	if err != nil || media != "application/json" {
		fail(w, fault.New("unsupported_media_type", "请发送 JSON 请求。"))
		return false
	}
	r.Body = http.MaxBytesReader(w, r.Body, 128<<10)
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
	status := map[string]int{"validation_error": 400, "invalid_json": 400, "unauthorized": 401, "invalid_credentials": 401, "forbidden": 403, "forbidden_origin": 403, "not_found": 404, "version_conflict": 409, "email_taken": 409, "idempotency_conflict": 409, "export_too_large": 413, "import_too_large": 413, "cover_too_large": 413, "cover_quota_exceeded": 413, "unsupported_media_type": 415, "rate_limited": 429, "media_busy": 503, "service_unavailable": 503, "plugin_failed": 422, "internal_error": 500}[problem.Code]
	if problem.Code == "invalid_email_token" {
		status = 400
	}
	if problem.Code == "invalid_oauth_state" {
		status = 400
	}
	if problem.Code == "storage_integrity" {
		status = 503
	}
	if problem.Code == "email_verification_required" {
		status = 403
	}
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
