package mediastore

import (
	"bytes"
	"context"
	"image"
	"image/png"
	"time"

	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/governance"
	"animemo.local/server/internal/id"
)

func (s *Store) Probe(ctx context.Context, actor string) error {
	if s.r2 == nil {
		return fault.New("service_unavailable", "R2 尚未配置。")
	}
	var buffer bytes.Buffer
	if err := png.Encode(&buffer, image.NewRGBA(image.Rect(0, 0, 1, 1))); err != nil {
		return err
	}
	data := buffer.Bytes()
	probe := id.New()
	var namespace string
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background())
	if err = governance.Authorize(ctx, tx, actor); err != nil {
		return err
	}
	if err = tx.QueryRow(ctx, `SELECT namespace FROM media_settings`).Scan(&namespace); err != nil {
		return err
	}
	key := "animemo/" + namespace + "/probes/" + probe
	if _, err = tx.Exec(ctx, `INSERT INTO media_objects(revision,owner_id,kind,target_id,backend_id,object_key,content_type,byte_size,sha256,state) VALUES($1,$2,'probe',$1,$3,$4,'image/png',$5,$6,'uploading')`, probe, actor, s.r2.ID, key, len(data), digest(data)); err != nil {
		return err
	}
	if err = tx.Commit(ctx); err != nil {
		return err
	}
	operation, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	err = s.r2.Put(operation, key, "image/png", data)
	if err == nil {
		err = s.r2.Delete(operation, key)
	}
	state := "deleted"
	if err != nil {
		state = "orphan"
	}
	cleanup, done := context.WithTimeout(context.Background(), 3*time.Second)
	defer done()
	_, saveErr := s.pool.Exec(cleanup, `UPDATE media_objects SET state=$2,available_at=now()+interval '2 minutes',updated_at=now() WHERE revision=$1`, probe, state)
	if err != nil {
		return err
	}
	return saveErr
}
