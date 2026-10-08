package main

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	"animemo.local/server/internal/accounts"
	"animemo.local/server/internal/api"
	"animemo.local/server/internal/bangumi"
	"animemo.local/server/internal/buildinfo"
	"animemo.local/server/internal/database"
	"animemo.local/server/internal/external"
	"animemo.local/server/internal/id"
	"animemo.local/server/internal/jobs"
	"animemo.local/server/internal/journal"
	"animemo.local/server/internal/mailer"
	"animemo.local/server/internal/mediastore"
	"animemo.local/server/internal/plugins"
)

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, nil)))
	if err := run(); err != nil {
		slog.Error("server stopped", "error", err)
		os.Exit(1)
	}
}

func run() error {
	if len(os.Args) > 1 && os.Args[1] == "version" {
		info, err := buildinfo.Current()
		if err != nil {
			return err
		}
		return json.NewEncoder(os.Stdout).Encode(info)
	}
	if len(os.Args) > 1 && os.Args[1] == "plugin-exec" {
		return plugins.ServeProcess(os.Stdin, os.Stdout)
	}
	address := os.Getenv("LISTEN_ADDR")
	if address == "" {
		address = "127.0.0.1:18081"
	}
	if len(os.Args) > 1 && os.Args[1] == "healthcheck" {
		_, port, err := net.SplitHostPort(address)
		if err != nil {
			return err
		}
		client := &http.Client{Timeout: 3 * time.Second}
		response, err := client.Get("http://127.0.0.1:" + port + "/api/ready")
		if err != nil {
			return err
		}
		defer response.Body.Close()
		if response.StatusCode != http.StatusOK {
			return fmt.Errorf("readiness returned HTTP %d", response.StatusCode)
		}
		return nil
	}
	databaseURL := os.Getenv("DATABASE_URL")
	if databaseURL == "" {
		return errors.New("DATABASE_URL is required; see README.md")
	}
	origin := os.Getenv("PUBLIC_ORIGIN")
	if origin == "" {
		origin = "http://127.0.0.1:5177"
	}
	parsed, err := url.Parse(origin)
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Host == "" || parsed.Path != "" || parsed.RawQuery != "" || parsed.Fragment != "" || parsed.User != nil {
		return errors.New("PUBLIC_ORIGIN must be an exact http(s) origin without a path")
	}
	if parsed.Scheme == "http" && parsed.Hostname() != "127.0.0.1" && parsed.Hostname() != "localhost" && parsed.Hostname() != "::1" {
		return errors.New("PUBLIC_ORIGIN requires HTTPS outside loopback development")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	pool, err := database.Open(ctx, databaseURL)
	if err != nil {
		return err
	}
	defer pool.Close()
	if len(os.Args) > 1 && os.Args[1] == "plugins-check" {
		if err = plugins.Preflight(ctx, pool); err != nil {
			return err
		}
		slog.Info("enabled plugin compatibility checked", "host_api", plugins.HostAPI)
		return nil
	}
	if err = database.Migrate(ctx, pool); err != nil {
		return err
	}
	if len(os.Args) > 1 && os.Args[1] == "migrate" {
		slog.Info("database migrations applied")
		return nil
	}
	if len(os.Args) > 1 && os.Args[1] == "media-restore" {
		if len(os.Args) != 3 {
			return errors.New("media-restore requires the media.zip path")
		}
		file, err := os.Open(os.Args[2])
		if err != nil {
			return err
		}
		defer file.Close()
		info, err := file.Stat()
		if err != nil {
			return err
		}
		return mediastore.New(pool, nil).Restore(context.Background(), file, info.Size())
	}
	r2, err := mediastore.NewR2(mediastore.R2Config{Endpoint: os.Getenv("R2_ENDPOINT"), Bucket: os.Getenv("R2_BUCKET"), AccessKeyID: os.Getenv("R2_ACCESS_KEY_ID"), SecretAccessKey: os.Getenv("R2_SECRET_ACCESS_KEY")}, nil)
	if err != nil {
		return err
	}
	storage := mediastore.New(pool, r2)
	if err = storage.Initialize(ctx); err != nil {
		return err
	}
	if len(os.Args) > 1 && os.Args[1] == "media-export" {
		return storage.Export(context.Background(), os.Stdout)
	}
	if len(os.Args) > 1 && os.Args[1] == "worker-health" {
		state, err := jobs.New(pool).Status(ctx)
		if err != nil {
			return err
		}
		if !state.WorkerReady {
			return errors.New("worker heartbeat unavailable")
		}
		return nil
	}
	if len(os.Args) > 1 && os.Args[1] != "serve" && os.Args[1] != "worker" {
		return fmt.Errorf("unknown command %q; use serve, migrate, plugins-check, media-export or media-restore", os.Args[1])
	}
	var secretKey []byte
	if encoded := os.Getenv("ANIMEMO_SECRET_KEY"); encoded != "" {
		secretKey, err = hex.DecodeString(encoded)
		if err != nil || len(secretKey) != 32 {
			return errors.New("ANIMEMO_SECRET_KEY must be 64 hexadecimal characters")
		}
	}
	mailClient, err := mailer.New(os.Getenv("RESEND_API_KEY"), os.Getenv("RESEND_FROM_EMAIL"), nil)
	if err != nil {
		return err
	}
	mailService := accounts.New(pool)
	if err = mailService.ConfigureEncryption(secretKey); err != nil {
		return err
	}
	if err = mailService.ConfigureMail(mailClient, origin); err != nil {
		return err
	}
	provider := bangumi.New(nil)
	externalService := external.New(pool, provider)
	oauth := bangumi.OAuthConfig{ClientID: os.Getenv("BANGUMI_OAUTH_CLIENT_ID"), ClientSecret: os.Getenv("BANGUMI_OAUTH_CLIENT_SECRET"), RedirectURI: os.Getenv("BANGUMI_OAUTH_REDIRECT_URI")}
	if err = externalService.ConfigureOAuth(oauth, secretKey, origin); err != nil {
		return err
	}
	stop, stopSignals := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stopSignals()
	if len(os.Args) > 1 && os.Args[1] == "worker" {
		service := jobs.New(pool)
		workerID := id.New()
		var wg sync.WaitGroup
		tasks := []func(context.Context){journal.New(pool).RunImportWorker, journal.New(pool).RunMemoryWorker, mailService.RunMailWorker, externalService.RunSyncWorker, storage.Run}
		for _, task := range tasks {
			wg.Add(1)
			go func(run func(context.Context)) { defer wg.Done(); run(stop) }(task)
		}
		if err = service.Heartbeat(stop, workerID); err != nil {
			return err
		}
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-stop.Done():
				wg.Wait()
				return nil
			case <-ticker.C:
				if err = service.Heartbeat(stop, workerID); err != nil {
					slog.Error("worker.heartbeat_failed", "worker_id", workerID)
				}
				for n := 0; n < 32; n++ {
					worked, e := service.ProcessNext(stop)
					if e != nil {
						slog.Error("worker.event_failed", "worker_id", workerID)
						break
					}
					if !worked {
						break
					}
				}
			}
		}
	}
	executable, err := os.Executable()
	if err != nil {
		return err
	}
	if err = plugins.EnsureBundled(ctx, pool, os.Getenv("ANIMEMO_BUNDLED_DIR"), executable); err != nil {
		slog.Error("bundled extensions unavailable; core remains available", "error", err)
		if _, err = pool.Exec(ctx, `UPDATE plugin_deployments SET enabled=false,health='quarantined',health_reason='随附扩展与当前发行不匹配，请检查安装包。',revision=revision+1 WHERE slug IN (SELECT slug FROM bundled_extensions)`); err != nil {
			return err
		}
	}
	if err = plugins.QuarantineInvalid(ctx, pool); err != nil {
		return err
	}
	pluginService := plugins.New(pool)
	pluginService.SetExecutor(plugins.ProcessExecutor(executable))
	server := &http.Server{Addr: address, Handler: api.New(pool, api.Config{PublicOrigin: strings.TrimRight(origin, "/"), WebDir: os.Getenv("WEB_DIR"), SetupToken: os.Getenv("ANIMEMO_SETUP_TOKEN"), SecretKey: secretKey, Mail: mailClient, External: externalService, Storage: storage, Plugins: pluginService}), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 15 * time.Second, WriteTimeout: 30 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 16 << 10}
	listener, err := net.Listen("tcp", address)
	if err != nil {
		return err
	}
	shutdownDone := make(chan struct{})
	go func() {
		defer close(shutdownDone)
		<-stop.Done()
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := server.Shutdown(ctx); err != nil {
			_ = server.Close()
		}
	}()
	slog.Info("server ready", "address", address, "public_origin", origin)
	if err = server.Serve(listener); !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	<-shutdownDone
	return nil
}
