package api

import (
	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/journal"
	"animemo.local/server/internal/media"
	"io"
	"net/http"
)

func (a *API) achievementRoutes(mux *http.ServeMux) {
	mux.HandleFunc("POST /api/v1/admin/achievements/images", a.admin(func(w http.ResponseWriter, r *http.Request) {
		select {
		case a.coverSlots <- struct{}{}:
			defer func() { <-a.coverSlots }()
		default:
			fail(w, fault.New("media_busy", "正在处理其他图片，请稍后重试。"))
			return
		}
		data, err := io.ReadAll(http.MaxBytesReader(w, r.Body, media.MaxBytes))
		if err != nil {
			fail(w, fault.Field("badge_image_id", "图案需为不超过 2 MiB 的 PNG 或 JPG 图片。"))
			return
		}
		out, err := a.journal.UploadAchievementImage(r.Context(), currentUser(r).ID, r.Header.Get("Content-Type"), data)
		libraryReply(w, 201, out, err)
	}))
	mux.HandleFunc("GET /api/v1/memory/achievement-images/{id}", a.require(func(w http.ResponseWriter, r *http.Request) {
		im, err := a.journal.AchievementImage(r.Context(), currentUser(r).ID, r.PathValue("id"))
		if err != nil {
			fail(w, err)
			return
		}
		w.Header().Set("Content-Type", "image/png")
		w.Header().Set("Cache-Control", "private, no-cache")
		w.Header().Set("ETag", `"`+im.ID+`"`)
		w.Header().Set("Cross-Origin-Resource-Policy", "same-origin")
		w.Header().Set("Content-Security-Policy", "default-src 'none'; sandbox")
		if r.Header.Get("If-None-Match") == `"`+im.ID+`"` {
			w.WriteHeader(304)
			return
		}
		w.Write(im.Data)
	}))
	mux.HandleFunc("GET /api/v1/memory/achievements", a.require(func(w http.ResponseWriter, r *http.Request) {
		out, err := a.journal.Achievements(r.Context(), currentUser(r).ID)
		libraryReply(w, 200, out, err)
	}))
	mux.HandleFunc("PUT /api/v1/memory/achievements/showcase", a.require(func(w http.ResponseWriter, r *http.Request) {
		var in struct {
			UnlockIDs []string `json:"unlock_ids"`
		}
		if !decode(w, r, &in) {
			return
		}
		if err := a.journal.AchievementShowcase(r.Context(), currentUser(r).ID, in.UnlockIDs); err != nil {
			fail(w, err)
			return
		}
		w.WriteHeader(204)
	}))
	mux.HandleFunc("POST /api/v1/memory/achievements/acknowledge", a.require(func(w http.ResponseWriter, r *http.Request) {
		if err := a.journal.AcknowledgeAchievements(r.Context(), currentUser(r).ID); err != nil {
			fail(w, err)
			return
		}
		w.WriteHeader(204)
	}))
	mux.HandleFunc("GET /api/v1/admin/achievements/rules", a.admin(func(w http.ResponseWriter, r *http.Request) {
		out, err := a.journal.AchievementRules(r.Context())
		libraryReply(w, 200, map[string]any{"items": out}, err)
	}))
	mux.HandleFunc("PUT /api/v1/admin/achievements/rules", a.admin(func(w http.ResponseWriter, r *http.Request) {
		var in journal.AchievementRule
		if !decode(w, r, &in) {
			return
		}
		out, err := a.journal.SaveAchievementRule(r.Context(), currentUser(r).ID, in)
		libraryReply(w, 200, out, err)
	}))
	mux.HandleFunc("POST /api/v1/admin/achievements/grants", a.admin(func(w http.ResponseWriter, r *http.Request) {
		var in journal.AchievementGrantInput
		if !decode(w, r, &in) {
			return
		}
		if err := a.journal.AdminAchievementGrant(r.Context(), currentUser(r).ID, in); err != nil {
			fail(w, err)
			return
		}
		w.WriteHeader(204)
	}))
	mux.HandleFunc("GET /api/v1/admin/achievements/backfills", a.admin(func(w http.ResponseWriter, r *http.Request) {
		out, err := a.journal.AchievementBackfills(r.Context())
		libraryReply(w, 200, map[string]any{"items": out}, err)
	}))
	mux.HandleFunc("POST /api/v1/admin/achievements/backfills", a.admin(func(w http.ResponseWriter, r *http.Request) {
		out, err := a.journal.PreviewAchievementBackfill(r.Context(), currentUser(r).ID)
		libraryReply(w, 201, out, err)
	}))
	mux.HandleFunc("POST /api/v1/admin/achievements/backfills/{id}", a.admin(func(w http.ResponseWriter, r *http.Request) {
		var in struct {
			Action string `json:"action"`
		}
		if !decode(w, r, &in) {
			return
		}
		out, err := a.journal.AchievementBackfillAction(r.Context(), currentUser(r).ID, r.PathValue("id"), in.Action)
		libraryReply(w, 200, out, err)
	}))
}
