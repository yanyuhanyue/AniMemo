package api

import (
	"net/http"
	"strconv"

	"animemo.local/server/internal/journal"
	"animemo.local/server/internal/media"
)

func (a *API) publication(w http.ResponseWriter, r *http.Request) {
	var input struct {
		Action  string `json:"action"`
		Version int    `json:"version"`
	}
	if !decode(w, r, &input) {
		return
	}
	u, err := a.accounts.Publication(r.Context(), currentUser(r).ID, input.Action, input.Version)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, u)
}
func (a *API) directory(w http.ResponseWriter, r *http.Request) {
	page := 1
	if r.URL.Query().Has("page") {
		page, _ = strconv.Atoi(r.URL.Query().Get("page"))
	}
	out, err := a.journal.Directory(r.Context(), r.URL.Query().Get("search"), page)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, out)
}
func (a *API) publicEntries(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	page := 1
	if q.Has("page") {
		page, _ = strconv.Atoi(q.Get("page"))
	}
	out, err := a.journal.PublicEntries(r.Context(), r.PathValue("slug"), journal.Filter{Search: q.Get("search"), Status: q.Get("status"), Sort: q.Get("sort"), Page: page, PageSize: 12})
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, out)
}
func (a *API) shared(w http.ResponseWriter, r *http.Request) {
	out, err := a.journal.Shared(r.Context(), r.PathValue("slug"))
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, out)
}
func writeImage(w http.ResponseWriter, picture media.Image) {
	w.Header().Set("Content-Type", picture.ContentType)
	w.Header().Set("Cross-Origin-Resource-Policy", "same-origin")
	w.Header().Set("Content-Security-Policy", "default-src 'none'; sandbox")
	w.Write(picture.Data)
}
func (a *API) publicCover(w http.ResponseWriter, r *http.Request) {
	picture, err := a.journal.PublicCover(r.Context(), r.PathValue("slug"), r.PathValue("revision"))
	if err != nil {
		fail(w, err)
		return
	}
	writeImage(w, picture)
}
func (a *API) publicAvatar(w http.ResponseWriter, r *http.Request) {
	picture, err := a.journal.PublicAvatar(r.Context(), r.PathValue("slug"), r.PathValue("revision"))
	if err != nil {
		fail(w, err)
		return
	}
	writeImage(w, picture)
}
func (a *API) resetShare(w http.ResponseWriter, r *http.Request) {
	var input struct {
		Version int `json:"version"`
	}
	if !decode(w, r, &input) {
		return
	}
	out, err := a.journal.ResetShare(r.Context(), currentUser(r).ID, r.PathValue("id"), input.Version)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, out)
}
