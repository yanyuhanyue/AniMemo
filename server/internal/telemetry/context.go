package telemetry

import (
	"context"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type requestKey struct{}

func WithRequestID(ctx context.Context, value string) context.Context {
	return context.WithValue(ctx, requestKey{}, value)
}
func RequestID(ctx context.Context) string {
	value, _ := ctx.Value(requestKey{}).(string)
	return value
}

// Transaction-local context never leaks through pooled database connections.
func Begin(ctx context.Context, pool *pgxpool.Pool, options ...pgx.TxOptions) (pgx.Tx, error) {
	var option pgx.TxOptions
	if len(options) > 0 {
		option = options[0]
	}
	tx, err := pool.BeginTx(ctx, option)
	if err != nil {
		return nil, err
	}
	if value := RequestID(ctx); value != "" {
		if _, err = tx.Exec(ctx, `SELECT set_config('animemo.request_id',$1,true)`, value); err != nil {
			tx.Rollback(context.Background())
			return nil, err
		}
	}
	return tx, nil
}
