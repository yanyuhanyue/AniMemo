package api

import (
	"net/http"
	"strconv"

	"animemo.local/server/internal/external"
	"animemo.local/server/internal/fault"
)

func (a *API) searchSubjects(w http.ResponseWriter, r *http.Request) {
	page := 1
	if raw := r.URL.Query().Get("page"); raw != "" {
		var err error
		page, err = strconv.Atoi(raw)
		if err != nil {
			fail(w, fault.Field("page", "页码无效。"))
			return
		}
	}
	out, err := a.external.Search(r.Context(), r.URL.Query().Get("query"), page)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, out)
}
func subjectID(w http.ResponseWriter, r *http.Request) (int64, bool) {
	value, err := strconv.ParseInt(r.PathValue("subject"), 10, 64)
	if err != nil || value < 1 || value > 2147483647 {
		fail(w, fault.Field("subject_id", "Bangumi 条目标识无效。"))
		return 0, false
	}
	return value, true
}
func (a *API) subject(w http.ResponseWriter, r *http.Request) {
	id, ok := subjectID(w, r)
	if !ok {
		return
	}
	out, err := a.external.Subject(r.Context(), id)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, out)
}
func (a *API) subjectCover(w http.ResponseWriter, r *http.Request) {
	id, ok := subjectID(w, r)
	if !ok {
		return
	}
	select {
	case a.coverSlots <- struct{}{}:
		defer func() { <-a.coverSlots }()
	default:
		fail(w, fault.New("media_busy", "图片正在处理，请稍后重试。"))
		return
	}
	out, err := a.external.Cover(r.Context(), id)
	if err != nil {
		fail(w, err)
		return
	}
	w.Header().Set("Content-Type", out.ContentType)
	w.Header().Set("Cross-Origin-Resource-Policy", "same-origin")
	w.Header().Set("Content-Security-Policy", "default-src 'none'; sandbox")
	w.Write(out.Data)
}
func (a *API) applySource(w http.ResponseWriter, r *http.Request) {
	var input external.ApplyInput
	if !decode(w, r, &input) {
		return
	}
	if r.PathValue("id") != "" {
		if _, err := a.journal.Get(r.Context(), currentUser(r).ID, r.PathValue("id")); err != nil {
			fail(w, err)
			return
		}
	}
	select {
	case a.coverSlots <- struct{}{}:
		defer func() { <-a.coverSlots }()
	default:
		fail(w, fault.New("media_busy", "资料正在处理，请稍后重试。"))
		return
	}
	out, err := a.external.Apply(r.Context(), currentUser(r).ID, r.PathValue("id"), input)
	if err != nil {
		fail(w, err)
		return
	}
	status := 200
	if r.PathValue("id") == "" {
		status = 201
	}
	write(w, status, out)
}
func (a *API) unbindSource(w http.ResponseWriter, r *http.Request) {
	version, ok := coverVersion(w, r)
	if !ok {
		return
	}
	out, err := a.journal.UnbindSource(r.Context(), currentUser(r).ID, r.PathValue("id"), version)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, out)
}
