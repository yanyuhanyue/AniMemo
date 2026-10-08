package journal

import "context"

// Remove abandoned reservations in bounded owner batches. Attached originals
// have no expiry; deleting a note alone never destroys its original media.
func (s *Service) CleanupMemoryMedia(ctx context.Context) error {
	rows, err := s.pool.Query(ctx, `SELECT DISTINCT owner_id FROM private_memory_media WHERE state<>'deleted' AND expires_at<now() LIMIT 10`)
	if err != nil {
		return err
	}
	owners := []string{}
	for rows.Next() {
		var owner string
		if err = rows.Scan(&owner); err != nil {
			rows.Close()
			return err
		}
		owners = append(owners, owner)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	for _, owner := range owners {
		tx, err := s.libraryTx(ctx, owner)
		if err != nil {
			return err
		}
		_, err = tx.Exec(ctx, `DELETE FROM private_memory_media m WHERE owner_id=$1 AND expires_at<now() AND NOT EXISTS(SELECT 1 FROM memory_notes n WHERE n.owner_id=$1 AND m.id=ANY(n.media_ids))`, owner)
		if err != nil {
			tx.Rollback(context.Background())
			return err
		}
		if err = tx.Commit(ctx); err != nil {
			return err
		}
	}
	return nil
}
