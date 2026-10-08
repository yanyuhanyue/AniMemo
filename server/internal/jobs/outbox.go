// Package jobs executes committed core events; it never owns journal transactions.
package jobs

import (
	"animemo.local/server/internal/id"
	"context"
	"errors"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"log/slog"
	"time"
)

type Claim struct {
	ID, Token, Kind, RequestID string
	Attempt                    int
}
type Service struct{ pool *pgxpool.Pool }

func New(pool *pgxpool.Pool) *Service { return &Service{pool: pool} }

func (s *Service) Claim(ctx context.Context) (Claim, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return Claim{}, err
	}
	defer tx.Rollback(context.Background())
	c := Claim{Token: id.New()}
	err = tx.QueryRow(ctx, `UPDATE core_outbox SET state='running',attempts=attempts+1,lease_token=$1,lease_until=now()+interval '30 seconds' WHERE id=(SELECT id FROM core_outbox WHERE (state='pending' AND available_at<=now()) OR (state='running' AND lease_until<now()) ORDER BY available_at,id FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING id,kind,attempts,request_id`, c.Token).Scan(&c.ID, &c.Kind, &c.Attempt, &c.RequestID)
	if err != nil {
		return c, err
	}
	if _, err = tx.Exec(ctx, `UPDATE job_attempts SET outcome='lease_expired',finished_at=now() WHERE job_id=$1 AND outcome='running';`, c.ID); err != nil {
		return c, err
	}
	if _, err = tx.Exec(ctx, `INSERT INTO job_attempts(job_id,attempt,lease_token) VALUES($1,$2,$3)`, c.ID, c.Attempt, c.Token); err != nil {
		return c, err
	}
	return c, tx.Commit(ctx)
}

// Completion checks the lease under the same lock as the effect and receipt.
// A stale executor cannot commit after another executor has reclaimed its job.
func (s *Service) Complete(ctx context.Context, c Claim) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background())
	var valid bool
	err = tx.QueryRow(ctx, `SELECT lease_token=$2 AND state='running' AND lease_until>now() FROM core_outbox WHERE id=$1 FOR UPDATE`, c.ID, c.Token).Scan(&valid)
	if err != nil {
		return err
	}
	if !valid {
		return errors.New("job lease lost")
	}
	if c.Kind != "memory.revision" {
		return errors.New("unsupported job kind")
	}
	if _, err = tx.Exec(ctx, `INSERT INTO memory_activity(revision_id,owner_id) SELECT id,owner_id FROM memory_revisions WHERE id=$1 ON CONFLICT DO NOTHING`, c.ID); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, `UPDATE core_outbox SET state='done',finished_at=now(),lease_until=NULL WHERE id=$1;`, c.ID); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, `UPDATE job_attempts SET outcome='done',finished_at=now() WHERE job_id=$1 AND lease_token=$2`, c.ID, c.Token); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (s *Service) ProcessNext(ctx context.Context) (bool, error) {
	c, err := s.Claim(ctx)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	err = s.Complete(ctx, c)
	slog.Info("job.attempt", "job_id", c.ID, "request_id", c.RequestID, "attempt", c.Attempt, "success", err == nil)
	if err != nil && ctx.Err() == nil {
		_, saveErr := s.pool.Exec(ctx, `WITH changed AS (UPDATE core_outbox SET state=CASE WHEN attempts>=5 THEN 'failed' ELSE 'pending' END,available_at=now()+interval '5 seconds'*attempts,lease_until=NULL WHERE id=$1 AND lease_token=$2 AND state='running' RETURNING id) UPDATE job_attempts SET outcome='failed',finished_at=now() WHERE job_id IN (SELECT id FROM changed) AND lease_token=$2`, c.ID, c.Token)
		if saveErr != nil {
			return true, saveErr
		}
	}
	return true, err
}

func (s *Service) Heartbeat(ctx context.Context, worker string) error {
	_, err := s.pool.Exec(ctx, `INSERT INTO worker_heartbeats(id) VALUES($1) ON CONFLICT(id) DO UPDATE SET updated_at=now()`, worker)
	return err
}

type Status struct {
	WorkerReady bool      `json:"worker_ready"`
	Pending     int       `json:"pending"`
	Running     int       `json:"running"`
	Failed      int       `json:"failed"`
	Done        int       `json:"done"`
	CheckedAt   time.Time `json:"checked_at"`
}

func (s *Service) Status(ctx context.Context) (Status, error) {
	out := Status{CheckedAt: time.Now().UTC()}
	err := s.pool.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM worker_heartbeats WHERE updated_at>now()-interval '30 seconds'),count(*) FILTER(WHERE state='pending'),count(*) FILTER(WHERE state='running'),count(*) FILTER(WHERE state='failed'),count(*) FILTER(WHERE state='done') FROM core_outbox`).Scan(&out.WorkerReady, &out.Pending, &out.Running, &out.Failed, &out.Done)
	return out, err
}
