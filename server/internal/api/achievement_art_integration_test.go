//go:build integration

package api

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"

	"animemo.local/server/internal/journal"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type achievementQueueBarrier struct {
	started, release chan struct{}
	once             sync.Once
}

func (b *achievementQueueBarrier) TraceQueryStart(ctx context.Context, _ *pgx.Conn, data pgx.TraceQueryStartData) context.Context {
	if strings.HasPrefix(data.SQL, "SELECT owner_id,generation FROM achievement_pending") {
		b.once.Do(func() {
			close(b.started)
			select {
			case <-b.release:
			case <-ctx.Done():
			}
		})
	}
	return ctx
}
func (*achievementQueueBarrier) TraceQueryEnd(context.Context, *pgx.Conn, pgx.TraceQueryEndData) {}

func TestAchievementPublicationDuringWorkerRead(t *testing.T) {
	pool := isolatedDatabase(t)
	base := coverServer(t, pool)
	client, owner := registerBrowser(t, base, "publish-race@example.test")
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	s := journal.New(pool)
	request(t, client, base, "POST", "/api/v1/entries", map[string]string{"title": "已满足门槛"}, 201)
	drainAchievementQueue(t, s)
	if _, err := pool.Exec(ctx, `UPDATE users SET is_admin=true WHERE id=$1`, owner.ID); err != nil {
		t.Fatal(err)
	}
	barrier := &achievementQueueBarrier{started: make(chan struct{}), release: make(chan struct{})}
	config := pool.Config()
	config.ConnConfig.Tracer = barrier
	workerPool, err := pgxpool.NewWithConfig(ctx, config)
	if err != nil {
		t.Fatal(err)
	}
	defer workerPool.Close()
	defer cancel()
	finished := make(chan error, 1)
	go func() { _, err := journal.New(workerPool).ProcessAchievements(ctx); finished <- err }()
	select {
	case <-barrier.started:
	case <-ctx.Done():
		t.Fatal("worker did not reach its queue read")
	}
	rule, err := s.SaveAchievementRule(ctx, owner.ID, journal.AchievementRule{SeriesID: "concurrent-publication", SeriesTitle: "并发发布", Tier: 1, Title: "无需再次记录", Badge: "ticket", Metric: "recorded_anime", Threshold: 1, Active: true})
	if err != nil {
		t.Fatal(err)
	}
	close(barrier.release)
	if err = <-finished; err != nil {
		t.Fatal(err)
	}
	center, err := s.Achievements(ctx, owner.ID)
	if err != nil {
		t.Fatal(err)
	}
	for _, a := range center.Items {
		if a.ID == rule.ID && a.Granted {
			return
		}
	}
	t.Fatal("worker consumed the new pending generation using stale rules")
}

func TestAchievementArtPublicationAndPortableHistory(t *testing.T) {
	ctx := context.Background()
	pool := isolatedDatabase(t)
	base := coverServer(t, pool)
	admin, au := registerBrowser(t, base, "art-admin@example.test")
	alice, user := registerBrowser(t, base, "art-owner@example.test")
	bob, _ := registerBrowser(t, base, "art-stranger@example.test")
	if _, err := pool.Exec(ctx, `UPDATE users SET is_admin=true WHERE id=$1`, au.ID); err != nil {
		t.Fatal(err)
	}
	s := journal.New(pool)
	upload := func(client *http.Client, kind string, data []byte, want int) journal.AchievementImage {
		t.Helper()
		r, err := http.NewRequest("POST", base+"/api/v1/admin/achievements/images", bytes.NewReader(data))
		if err != nil {
			t.Fatal(err)
		}
		r.Header.Set("Origin", base)
		r.Header.Set("Content-Type", kind)
		res, err := client.Do(r)
		if err != nil {
			t.Fatal(err)
		}
		defer res.Body.Close()
		body, err := io.ReadAll(res.Body)
		if err != nil {
			t.Fatal(err)
		}
		if res.StatusCode != want {
			t.Fatalf("upload: want %d got %d: %s", want, res.StatusCode, body)
		}
		var out journal.AchievementImage
		if want == 201 {
			if err = json.Unmarshal(body, &out); err != nil {
				t.Fatal(err)
			}
		}
		return out
	}
	imageBytes := coverFixture(t, "image/png")
	upload(alice, "image/png", imageBytes, 403)
	upload(browser(), "image/png", imageBytes, 401)
	upload(admin, "image/svg+xml", []byte(`<svg xmlns="http://www.w3.org/2000/svg"/>`), 415)
	upload(admin, "image/png", []byte("not an image"), 400)
	upload(admin, "image/png", make([]byte, (2<<20)+1), 400)
	im := upload(admin, "image/png", imageBytes, 201)
	if len(im.ID) != 64 || im.Width > 256 || im.Height > 256 || im.Data != nil {
		t.Fatal("invalid normalized image metadata")
	}
	if upload(admin, "image/png", imageBytes, 201).ID != im.ID {
		t.Fatal("identical uploads were not deduplicated")
	}
	imagePath := "/api/v1/memory/achievement-images/" + im.ID
	normalized := request(t, admin, base, "GET", imagePath, nil, 200)
	request(t, alice, base, "GET", imagePath, nil, 404)
	request(t, browser(), base, "GET", imagePath, nil, 401)
	if normalized.header.Get("Content-Type") != "image/png" || normalized.header.Get("ETag") == "" {
		t.Fatal("image delivery headers missing")
	}
	// Existing users meet the future rule, then stop writing records.
	request(t, alice, base, "POST", "/api/v1/entries", map[string]string{"title": "早已记下的故事"}, 201)
	drainAchievementQueue(t, s)
	rule := journal.AchievementRule{SeriesID: "art-review", SeriesTitle: "故事纪念", Tier: 1, Title: "我的青铜纪念", Badge: "ticket", BadgeImageID: im.ID, Metric: "recorded_anime", Threshold: 1}
	rule = decodeAs[journal.AchievementRule](t, request(t, admin, base, "PUT", "/api/v1/admin/achievements/rules", rule, 200))
	if worked, err := s.ProcessAchievements(ctx); err != nil || worked {
		t.Fatal("inactive draft should not queue old users", err)
	}
	rule.Active = true
	rule = decodeAs[journal.AchievementRule](t, request(t, admin, base, "PUT", "/api/v1/admin/achievements/rules", rule, 200))
	drainAchievementQueue(t, s)
	center, err := s.Achievements(ctx, user.ID)
	if err != nil {
		t.Fatal(err)
	}
	var award journal.Achievement
	for _, a := range center.Items {
		if a.ID == rule.ID {
			award = a
		}
	}
	if !award.Granted || award.Value != 1 || award.Revision != 2 {
		t.Fatal("activation did not award an idle, already eligible user")
	}
	if !bytes.Equal(request(t, alice, base, "GET", imagePath, nil, 200).body, normalized.body) {
		t.Fatal("normalized artwork changed")
	}
	request(t, bob, base, "GET", imagePath, nil, 200) // Current published artwork is visible with its rule.
	if _, err = pool.Exec(ctx, `UPDATE achievement_images SET created_at=now()-interval '2 days' WHERE id=$1`, im.ID); err != nil {
		t.Fatal(err)
	}
	secondImage := upload(admin, "image/jpeg", coverFixture(t, "image/jpeg"), 201)
	rule.BadgeImageID = secondImage.ID
	rule = decodeAs[journal.AchievementRule](t, request(t, admin, base, "PUT", "/api/v1/admin/achievements/rules", rule, 200))
	if worked, err := s.ProcessAchievements(ctx); err != nil || worked {
		t.Fatal("art-only edits should not rescan every journal", err)
	}
	request(t, alice, base, "GET", imagePath, nil, 200) // The original award pins its original image.
	request(t, bob, base, "GET", imagePath, nil, 404)
	// Newly published tiers also process users without any new activity.
	second := rule
	second.ID = ""
	second.Revision = 0
	second.Tier = 2
	second.Threshold = 2
	second.Title = "白银纪念"
	second = decodeAs[journal.AchievementRule](t, request(t, admin, base, "PUT", "/api/v1/admin/achievements/rules", second, 200))
	drainAchievementQueue(t, s)
	second.Threshold = 1
	second = decodeAs[journal.AchievementRule](t, request(t, admin, base, "PUT", "/api/v1/admin/achievements/rules", second, 200))
	drainAchievementQueue(t, s)
	center, err = s.Achievements(ctx, user.ID)
	if err != nil {
		t.Fatal(err)
	}
	var secondAward journal.Achievement
	for _, a := range center.Items {
		if a.ID == second.ID {
			secondAward = a
		}
		if a.ID == rule.ID && (a.Revision != 2 || a.BadgeImageID != im.ID) {
			t.Fatal("image replacement rewrote earned art")
		}
	}
	if !secondAward.Granted {
		t.Fatal("lowered threshold did not award existing user")
	}
	if err = s.AchievementShowcase(ctx, user.ID, []string{award.UnlockID}); err != nil {
		t.Fatal(err)
	}
	if err = s.AdminAchievementGrant(ctx, au.ID, journal.AchievementGrantInput{OwnerID: user.ID, TierID: second.ID, Grant: false, Reason: "验证自动评估不重授"}); err != nil {
		t.Fatal(err)
	}
	second.Active = false
	second, err = s.SaveAchievementRule(ctx, au.ID, second)
	if err != nil {
		t.Fatal(err)
	}
	second.Active = true
	if _, err = s.SaveAchievementRule(ctx, au.ID, second); err != nil {
		t.Fatal(err)
	}
	drainAchievementQueue(t, s)
	exported, err := s.Export(ctx, user.ID)
	if err != nil {
		t.Fatal(err)
	}
	if len(exported.Library.Achievements.Images) != 2 {
		t.Fatal("export did not carry both historical artwork files")
	}
	for _, a := range exported.Library.Achievements.Unlocks {
		if a.Rule.ID == second.ID && a.Granted {
			t.Fatal("reactivation undid a revocation")
		}
	}
	// Restore into a separate database which has neither these images nor rules.
	targetPool := isolatedDatabase(t)
	targetBase := coverServer(t, targetPool)
	targetService := journal.New(targetPool)
	for _, format := range []string{"json", "zip"} {
		t.Run(format, func(t *testing.T) {
			client, owner := registerBrowser(t, targetBase, "art-restore-"+format+"@example.test")
			data, err := json.Marshal(exported)
			if err != nil {
				t.Fatal(err)
			}
			if format == "zip" {
				data, err = s.Backup(ctx, user.ID)
				if err != nil {
					t.Fatal(err)
				}
			}
			job := importFile(t, client, targetBase, format, data, 202)
			if _, err = targetService.ProcessNextImport(ctx); err != nil {
				t.Fatal(err)
			}
			job, err = targetService.Import(ctx, owner.ID, job.ID)
			if err != nil || job.State != "ready" {
				t.Fatalf("preview: %s %s %v", job.State, job.Error, err)
			}
			if _, err = targetService.ImportAction(ctx, owner.ID, job.ID, "apply"); err != nil {
				t.Fatal(err)
			}
			if _, err = targetService.ProcessNextImport(ctx); err != nil {
				t.Fatal(err)
			}
			job, err = targetService.Import(ctx, owner.ID, job.ID)
			if err != nil || job.State != "done" {
				t.Fatalf("restore: %s %s %v", job.State, job.Error, err)
			}
			got, err := targetService.Export(ctx, owner.ID)
			if err != nil {
				t.Fatal(err)
			}
			if len(got.Library.Achievements.Images) != 2 {
				t.Fatal("restored artwork missing")
			}
			if !bytes.Equal(request(t, client, targetBase, "GET", imagePath, nil, 200).body, normalized.body) {
				t.Fatal("portable image bytes changed")
			}
			found := false
			for _, a := range got.Library.Achievements.Unlocks {
				if a.Rule.Title == award.Title {
					found = a.Granted && a.ShowcaseSlot == 1 && a.Rule.BadgeImageID == im.ID && a.Rule.Revision == award.Revision && a.UnlockedAt.Equal(*award.UnlockedAt) && !a.Rule.Active && strings.HasPrefix(a.Rule.SeriesID, "archive.")
				}
			}
			if !found {
				t.Fatal("portable custom art changed award semantics or activated imported rules")
			}
		})
	}
	// Corrupt image bytes must be rejected during preview, before core writes.
	exported.Library.Achievements.Images[0].Data[0] ^= 1
	data, _ := json.Marshal(exported)
	job := importFile(t, bob, base, "json", data, 202)
	if _, err = s.ProcessNextImport(ctx); err != nil {
		t.Fatal(err)
	}
	var bobID string
	if err = pool.QueryRow(ctx, `SELECT id FROM users WHERE email='art-stranger@example.test'`).Scan(&bobID); err != nil {
		t.Fatal(err)
	}
	job, err = s.Import(ctx, bobID, job.ID)
	if err != nil || job.State != "failed" {
		t.Fatal("corrupt achievement image accepted", err)
	}
}
