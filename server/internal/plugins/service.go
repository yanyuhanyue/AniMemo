package plugins

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/governance"
	"animemo.local/server/internal/id"
	"animemo.local/server/pkg/pluginproto"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type Service struct {
	execute Executor
	pool    *pgxpool.Pool
	slots   chan struct{}
}

func New(pool *pgxpool.Pool) *Service {
	return &Service{pool: pool, execute: Execute, slots: make(chan struct{}, 2)}
}
func (s *Service) SetExecutor(executor Executor) { s.execute = executor }
func (s *Service) acquire() (func(), error) {
	select {
	case s.slots <- struct{}{}:
		return func() { <-s.slots }, nil
	default:
		return nil, fault.New("media_busy", "插件正在处理其他文件，请稍后重试。")
	}
}

type Release struct {
	PublisherID    string               `json:"publisher_id"`
	Distribution   string               `json:"distribution"`
	InstallationID string               `json:"installation_id"`
	Health         string               `json:"health"`
	HealthReason   string               `json:"health_reason"`
	Manifest       pluginproto.Manifest `json:"manifest"`
	Digest         string               `json:"digest"`
	Active         bool                 `json:"active"`
	Enabled        bool                 `json:"enabled"`
	Revision       int64                `json:"revision"`
	InstalledAt    time.Time            `json:"installed_at"`
}

func (s *Service) List(ctx context.Context, all bool) ([]Release, error) {
	rows, err := s.pool.Query(ctx, `SELECT r.manifest,r.digest,r.version=d.active_version,d.enabled,d.revision,r.installed_at,r.publisher_id,r.distribution,d.installation_id,d.health,d.health_reason FROM plugin_releases r JOIN plugin_deployments d USING(slug) WHERE $1 OR (d.enabled AND d.active_version=r.version) ORDER BY r.slug,r.installed_at DESC`, all)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Release{}
	for rows.Next() {
		var r Release
		if err := rows.Scan(&r.Manifest, &r.Digest, &r.Active, &r.Enabled, &r.Revision, &r.InstalledAt, &r.PublisherID, &r.Distribution, &r.InstallationID, &r.Health, &r.HealthReason); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

func (s *Service) Install(ctx context.Context, actor string, data []byte) error {
	release, err := s.acquire()
	if err != nil {
		return err
	}
	defer release()
	p, digest, err := ParsePackage(data)
	if err != nil {
		return err
	}
	if err := validatePackageRuntime(ctx, p.Manifest, p.Module); err != nil {
		return err
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background())
	if err := governance.Authorize(ctx, tx, actor); err != nil {
		return err
	}
	var previous string
	err = tx.QueryRow(ctx, `SELECT digest FROM plugin_releases WHERE slug=$1 AND version=$2`, p.Manifest.Slug, p.Manifest.Version).Scan(&previous)
	if err == nil {
		if previous != digest {
			return fault.New("version_conflict", "同一插件版本的内容不可改写，请递增版本。")
		}
		return tx.Commit(ctx) // Idempotent upload never activates or grants anything.
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return err
	}
	var count, bytes int64
	if err := tx.QueryRow(ctx, `SELECT count(*),COALESCE(sum(octet_length(module)),0) FROM plugin_releases`).Scan(&count, &bytes); err != nil {
		return err
	}
	if count >= 32 || bytes+int64(len(p.Module)) > 256<<20 {
		return invalid("最多保留 32 个插件版本、合计 256 MiB；当前版本尚不提供历史包清理。")
	}
	if _, err := tx.Exec(ctx, `INSERT INTO plugin_releases(slug,version,digest,manifest,module) VALUES($1,$2,$3,$4,$5)`, p.Manifest.Slug, p.Manifest.Version, digest, p.Manifest, p.Module); err != nil {
		return err
	}
	if _, err := tx.Exec(ctx, `INSERT INTO plugin_deployments(slug,active_version) VALUES($1,$2) ON CONFLICT DO NOTHING`, p.Manifest.Slug, p.Manifest.Version); err != nil {
		return err
	}
	if err := governance.Audit(ctx, tx, actor, "plugin.install", "plugin", p.Manifest.Slug, map[string]string{"version": p.Manifest.Version, "digest": digest}); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

type Action struct {
	Action   string `json:"action"`
	Version  string `json:"version"`
	Revision int64  `json:"revision"`
}

func (s *Service) Change(ctx context.Context, actor, slug string, input Action) error {
	if !slugPattern.MatchString(slug) || (input.Action != "activate" && input.Action != "disable" && input.Action != "uninstall") || input.Revision < 1 {
		return invalid("插件操作无效。")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background())
	if err := governance.Authorize(ctx, tx, actor); err != nil {
		return err
	}
	var revision int64
	if err := tx.QueryRow(ctx, `SELECT revision FROM plugin_deployments WHERE slug=$1 FOR UPDATE`, slug).Scan(&revision); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return fault.New("not_found", "插件不存在。")
		}
		return err
	}
	if revision != input.Revision {
		return fault.New("version_conflict", "插件状态已变更，请刷新后重试。")
	}
	if input.Action == "uninstall" {
		var bundled bool
		if err = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM bundled_extensions WHERE slug=$1)`, slug).Scan(&bundled); err != nil {
			return err
		}
		if bundled {
			return invalid("随附扩展属于当前发行基线，请使用停用。")
		}
		if _, err = tx.Exec(ctx, `DELETE FROM plugin_deployments WHERE slug=$1`, slug); err != nil {
			return err
		}
		if _, err = tx.Exec(ctx, `DELETE FROM plugin_releases WHERE slug=$1`, slug); err != nil {
			return err
		}
		if err = governance.Audit(ctx, tx, actor, "plugin.uninstall", "plugin", slug, input); err != nil {
			return err
		}
		return tx.Commit(ctx)
	}
	if input.Action == "activate" {
		var m pluginproto.Manifest
		var module []byte
		var digest string
		if err := tx.QueryRow(ctx, `SELECT manifest,module,digest FROM plugin_releases WHERE slug=$1 AND version=$2`, slug, input.Version).Scan(&m, &module, &digest); err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				return fault.New("not_found", "插件版本不存在。")
			}
			return err
		}
		if err := Compatible(m); err != nil {
			return err
		}
		canonical, _ := json.Marshal(m)
		if fmt.Sprintf("%x", sha256.Sum256(canonical)) != digest || fmt.Sprintf("%x", sha256.Sum256(module)) != m.ModuleSHA256 {
			return invalid("扩展完整性检查失败，请重新安装已审阅的包。")
		}
		if err := validatePackageRuntime(ctx, m, module); err != nil {
			return err
		}
		_, err = tx.Exec(ctx, `UPDATE plugin_deployments SET health='ready',health_reason='',active_version=$2,enabled=true,revision=revision+1,updated_at=now() WHERE slug=$1`, slug, input.Version)
	} else {
		_, err = tx.Exec(ctx, `UPDATE plugin_deployments SET enabled=false,revision=revision+1,updated_at=now() WHERE slug=$1`, slug)
	}
	if err != nil {
		return err
	}
	if err := governance.Audit(ctx, tx, actor, "plugin."+input.Action, "plugin", slug, input); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (s *Service) Convert(ctx context.Context, actor, slug string, input pluginproto.Request) (out pluginproto.Response, err error) {
	if !id.Valid(actor) {
		return out, fault.New("unauthorized", "缺少调用者身份。")
	}
	if !slugPattern.MatchString(slug) {
		return out, fault.New("not_found", "插件不存在。")
	}
	release, err := s.acquire()
	if err != nil {
		return out, err
	}
	defer release()
	var module []byte
	var m pluginproto.Manifest
	var revision int64
	var installation, digest string
	err = s.pool.QueryRow(ctx, `SELECT r.module,r.manifest,d.revision,d.installation_id,r.digest FROM plugin_deployments d JOIN plugin_releases r ON r.slug=d.slug AND r.version=d.active_version WHERE d.slug=$1 AND d.enabled`, slug).Scan(&module, &m, &revision, &installation, &digest)
	if errors.Is(err, pgx.ErrNoRows) {
		return out, fault.New("not_found", "插件未启用。")
	}
	if err != nil {
		return out, err
	}
	if err := Compatible(m); err != nil {
		return out, err
	}
	if m.Capabilities[0] != "import.convert" {
		return out, invalid("此扩展不是文件转换器。")
	}
	callID := id.New()
	if _, err = s.pool.Exec(ctx, `INSERT INTO plugin_invocations(id,actor_id,installation_id,package_digest,capability) VALUES($1,$2,$3,$4,'import.convert')`, callID, actor, installation, digest); err != nil {
		return out, err
	}
	defer func() {
		cleanup, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		outcome := "done"
		if err != nil {
			outcome = "failed"
		}
		_, _ = s.pool.Exec(cleanup, `UPDATE plugin_invocations SET outcome=$2,finished_at=now() WHERE id=$1`, callID, outcome)
	}()
	out, err = s.execute(ctx, module, input)
	if err != nil {
		return out, err
	}
	var current bool
	if err := s.pool.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM plugin_deployments WHERE slug=$1 AND enabled AND revision=$2)`, slug, revision).Scan(&current); err != nil {
		return out, err
	}
	if !current {
		return out, fault.New("version_conflict", "运行期间插件已停用或切换版本，结果已丢弃。")
	}
	return out, nil
}

// Preflight is read-only and runs before migrations, including on an old DB
// without plugin tables. Disabled packages never block host recovery.
func Preflight(ctx context.Context, pool *pgxpool.Pool) error {
	var exists bool
	if err := pool.QueryRow(ctx, `SELECT to_regclass('plugin_deployments') IS NOT NULL`).Scan(&exists); err != nil {
		return err
	}
	if !exists {
		return nil
	}
	rows, err := pool.Query(ctx, `SELECT r.slug,r.version,r.digest,r.manifest,r.module FROM plugin_deployments d JOIN plugin_releases r ON r.slug=d.slug AND r.version=d.active_version WHERE d.enabled ORDER BY r.slug`)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var slug, version, digest string
		var manifest []byte
		var module []byte
		if err := rows.Scan(&slug, &version, &digest, &manifest, &module); err != nil {
			return err
		}
		var m pluginproto.Manifest
		if err := strictJSON(manifest, &m); err != nil {
			return fmt.Errorf("plugin %s@%s: invalid manifest", slug, version)
		}
		canonical, _ := json.Marshal(m)
		if err := Compatible(m); err != nil {
			return fmt.Errorf("plugin %s@%s incompatible: %w", slug, version, err)
		}
		if fmt.Sprintf("%x", sha256.Sum256(canonical)) != digest || fmt.Sprintf("%x", sha256.Sum256(module)) != m.ModuleSHA256 {
			return fmt.Errorf("plugin %s@%s: package checksum mismatch", slug, version)
		}
		if err := validatePackageRuntime(ctx, m, module); err != nil {
			return fmt.Errorf("plugin %s@%s: %w", slug, version, err)
		}
	}
	return rows.Err()
}
