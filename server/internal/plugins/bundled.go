package plugins

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"github.com/jackc/pgx/v5/pgxpool"
	"os"
	"path/filepath"
)

type BundleInventory struct {
	Schema     string `json:"schema"`
	CoreSHA256 string `json:"core_sha256"`
	Packages   []struct {
		File   string `json:"file"`
		SHA256 string `json:"sha256"`
	} `json:"packages"`
}

// Only packages bound to this exact executable receive first-party identity.
// An uploaded manifest cannot claim this publisher or replace a bundled version.
func EnsureBundled(ctx context.Context, pool *pgxpool.Pool, directory, executable string) error {
	if directory == "" {
		return nil
	}
	raw, err := os.ReadFile(filepath.Join(directory, "bundled-extensions.json"))
	if err != nil {
		return err
	}
	var inventory BundleInventory
	if err = strictJSON(raw, &inventory); err != nil {
		return err
	}
	binary, err := os.ReadFile(executable)
	if err != nil {
		return err
	}
	if inventory.Schema != "animemo.bundled/v1" || fmt.Sprintf("%x", sha256.Sum256(binary)) != inventory.CoreSHA256 || len(inventory.Packages) > 8 {
		return fmt.Errorf("bundled inventory does not match core")
	}
	tx, err := pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background())
	for _, item := range inventory.Packages {
		if filepath.Base(item.File) != item.File {
			return fmt.Errorf("invalid bundled path")
		}
		data, err := os.ReadFile(filepath.Join(directory, item.File))
		if err != nil {
			return err
		}
		if fmt.Sprintf("%x", sha256.Sum256(data)) != item.SHA256 {
			return fmt.Errorf("bundled package checksum mismatch")
		}
		p, digest, err := ParsePackage(data)
		if err != nil {
			return err
		}
		if err = validatePackageRuntime(ctx, p.Manifest, p.Module); err != nil {
			return err
		}
		var existing string
		if err = tx.QueryRow(ctx, `INSERT INTO plugin_releases(slug,version,digest,manifest,module,publisher_id,distribution) VALUES($1,$2,$3,$4,$5,'ANIMEMO_FIRST_PARTY','bundled') ON CONFLICT(slug,version) DO UPDATE SET slug=excluded.slug RETURNING digest`, p.Manifest.Slug, p.Manifest.Version, digest, p.Manifest, p.Module).Scan(&existing); err != nil {
			return err
		}
		if existing != digest {
			return fmt.Errorf("bundled package conflicts with installed identity")
		}
		if _, err = tx.Exec(ctx, `UPDATE plugin_releases SET publisher_id='ANIMEMO_FIRST_PARTY',distribution='bundled' WHERE slug=$1 AND version=$2`, p.Manifest.Slug, p.Manifest.Version); err != nil {
			return err
		}
		if _, err = tx.Exec(ctx, `INSERT INTO bundled_extensions(slug,version,digest,core_sha256) VALUES($1,$2,$3,$4) ON CONFLICT(slug) DO UPDATE SET version=excluded.version,digest=excluded.digest,core_sha256=excluded.core_sha256`, p.Manifest.Slug, p.Manifest.Version, digest, inventory.CoreSHA256); err != nil {
			return err
		}
		if _, err = tx.Exec(ctx, `INSERT INTO plugin_deployments(slug,active_version) VALUES($1,$2) ON CONFLICT DO NOTHING`, p.Manifest.Slug, p.Manifest.Version); err != nil {
			return err
		}
	}
	return tx.Commit(ctx)
}

// Core recovery remains available even when an optional package is damaged.
func QuarantineInvalid(ctx context.Context, pool *pgxpool.Pool) error {
	rows, err := pool.Query(ctx, `SELECT r.slug,r.manifest,r.module,r.digest FROM plugin_deployments d JOIN plugin_releases r ON r.slug=d.slug AND r.version=d.active_version WHERE d.enabled`)
	if err != nil {
		return err
	}
	type invalidPackage struct{ slug string }
	invalid := []invalidPackage{}
	for rows.Next() {
		var slug, digest string
		var manifest json.RawMessage
		var module []byte
		if err = rows.Scan(&slug, &manifest, &module, &digest); err != nil {
			rows.Close()
			return err
		}
		data, _ := json.Marshal(struct {
			Manifest json.RawMessage `json:"manifest"`
			Module   []byte          `json:"module"`
		}{manifest, module})
		parsed, actual, check := ParsePackage(data)
		if check == nil && actual == digest {
			check = validatePackageRuntime(ctx, parsed.Manifest, module)
		}
		if check != nil || actual != digest {
			invalid = append(invalid, invalidPackage{slug})
		}
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	for _, p := range invalid {
		if _, err = pool.Exec(ctx, `UPDATE plugin_deployments SET enabled=false,health='quarantined',health_reason='包不兼容或完整性检查失败，请重新审阅。',revision=revision+1 WHERE slug=$1`, p.slug); err != nil {
			return err
		}
	}
	return nil
}
