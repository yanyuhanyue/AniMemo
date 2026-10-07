package api

import (
	"animemo.local/server/internal/journal"
	"net/http"
	"strconv"
)

func (a *API) bulk(w http.ResponseWriter, r *http.Request) {
	var input journal.BulkInput
	if !decode(w, r, &input) {
		return
	}
	out, err := a.journal.Bulk(r.Context(), currentUser(r).ID, input)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, map[string]any{"items": out})
}
func (a *API) tags(w http.ResponseWriter, r *http.Request) {
	out, err := a.journal.Tags(r.Context(), currentUser(r).ID)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, map[string]any{"items": out})
}
func (a *API) setTag(w http.ResponseWriter, r *http.Request) {
	var input journal.Tag
	if !decode(w, r, &input) {
		return
	}
	if err := a.journal.SetTag(r.Context(), currentUser(r).ID, input); err != nil {
		fail(w, err)
		return
	}
	w.WriteHeader(204)
}
func (a *API) filters(w http.ResponseWriter, r *http.Request) {
	out, err := a.journal.Filters(r.Context(), currentUser(r).ID)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, map[string]any{"items": out})
}
func (a *API) saveFilter(w http.ResponseWriter, r *http.Request) {
	var input journal.QuickFilter
	if !decode(w, r, &input) {
		return
	}
	out, err := a.journal.SaveFilter(r.Context(), currentUser(r).ID, input)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 201, out)
}
func (a *API) deleteFilter(w http.ResponseWriter, r *http.Request) {
	if err := a.journal.DeleteFilter(r.Context(), currentUser(r).ID, r.PathValue("id")); err != nil {
		fail(w, err)
		return
	}
	w.WriteHeader(204)
}
func (a *API) historyPage(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	page := 1
	if q.Has("page") {
		page, _ = strconv.Atoi(q.Get("page"))
	}
	out, err := a.journal.HistoryPage(r.Context(), currentUser(r).ID, q.Get("entry_id"), q.Get("from"), q.Get("to"), page)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, out)
}
func (a *API) updateRecord(w http.ResponseWriter, r *http.Request) {
	var input journal.RecordPatch
	if !decode(w, r, &input) {
		return
	}
	out, err := a.journal.ChangeRecord(r.Context(), currentUser(r).ID, r.PathValue("id"), r.PathValue("record"), input.Version, &input)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, out)
}
func (a *API) deleteRecord(w http.ResponseWriter, r *http.Request) {
	version, ok := coverVersion(w, r)
	if !ok {
		return
	}
	out, err := a.journal.ChangeRecord(r.Context(), currentUser(r).ID, r.PathValue("id"), r.PathValue("record"), version, nil)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, out)
}
func (a *API) analytics(w http.ResponseWriter, r *http.Request) {
	out, err := a.journal.Analytics(r.Context(), currentUser(r).ID, r.URL.Query().Get("from"), r.URL.Query().Get("to"))
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, out)
}
