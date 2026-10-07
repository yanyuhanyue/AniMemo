package main

import (
	"context"
	"encoding/hex"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"animemo.local/server/internal/accounts"
	"animemo.local/server/internal/api"
	"animemo.local/server/internal/bangumi"
	"animemo.local/server/internal/database"
	"animemo.local/server/internal/external"
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
	if err = plugins.Preflight(ctx, pool); err != nil {
		return err
	}
	if len(os.Args) > 1 && os.Args[1] == "plugins-check" {
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
	if len(os.Args) > 1 && os.Args[1] != "serve" {
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
	server := &http.Server{Addr: address, Handler: api.New(pool, api.Config{PublicOrigin: strings.TrimRight(origin, "/"), WebDir: os.Getenv("WEB_DIR"), SetupToken: os.Getenv("ANIMEMO_SETUP_TOKEN"), SecretKey: secretKey, Mail: mailClient, External: externalService, Storage: storage}), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 15 * time.Second, WriteTimeout: 30 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 16 << 10}
	listener, err := net.Listen("tcp", address)
	if err != nil {
		return err
	}
	stop, stopSignals := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stopSignals()
	workerDone := make(chan struct{})
	go func() { defer close(workerDone); journal.New(pool).RunImportWorker(stop) }()
	mailDone := make(chan struct{})
	go func() { defer close(mailDone); mailService.RunMailWorker(stop) }()
	syncDone := make(chan struct{})
	go func() { defer close(syncDone); externalService.RunSyncWorker(stop) }()
	mediaDone := make(chan struct{})
	go func() { defer close(mediaDone); storage.Run(stop) }()
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
	<-workerDone
	<-mailDone
	<-syncDone
	<-mediaDone
	return nil
}
