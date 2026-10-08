package api

import (
	"animemo.local/server/internal/journal"
	"net/http"
)

func (a *API) achievementRoutes(mux *http.ServeMux) {
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
