package api

import (
	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/journal"
	"animemo.local/server/internal/media"
	"io"
	"net/http"
	"strconv"
)

func (a *API) columns(w http.ResponseWriter, r *http.Request) {
	out, err := a.journal.Columns(r.Context(), currentUser(r).ID, queryPage(r))
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, out)
}
func (a *API) column(w http.ResponseWriter, r *http.Request) {
	out, err := a.journal.Column(r.Context(), currentUser(r).ID, r.PathValue("id"))
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, out)
}
func (a *API) saveColumn(w http.ResponseWriter, r *http.Request) {
	var input journal.ColumnInput
	if !decode(w, r, &input) {
		return
	}
	out, err := a.journal.SaveColumn(r.Context(), currentUser(r).ID, r.PathValue("id"), input)
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
func (a *API) columnAction(w http.ResponseWriter, r *http.Request) {
	var input struct {
		Action  string `json:"action"`
		Version int    `json:"version"`
	}
	if !decode(w, r, &input) {
		return
	}
	out, err := a.journal.ColumnAction(r.Context(), currentUser(r).ID, r.PathValue("id"), input.Action, input.Version)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, out)
}
func (a *API) setColumnCover(w http.ResponseWriter, r *http.Request) {
	select {
	case a.coverSlots <- struct{}{}:
		defer func() { <-a.coverSlots }()
	default:
		fail(w, fault.New("media_busy", "图片正在处理中，请稍后重试。"))
		return
	}
	version, err := strconv.Atoi(r.URL.Query().Get("version"))
	if err != nil {
		fail(w, fault.Field("version", "缺少版本。"))
		return
	}
	var data []byte
	if r.Method == "PUT" {
		r.Body = http.MaxBytesReader(w, r.Body, media.MaxBytes)
		data, err = io.ReadAll(r.Body)
		if err != nil {
			fail(w, fault.New("cover_too_large", "图片不能超过 2 MB。"))
			return
		}
	}
	out, err := a.journal.SetColumnCover(r.Context(), currentUser(r).ID, r.PathValue("id"), version, data, r.Header.Get("Content-Type"))
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, out)
}
func (a *API) columnCover(w http.ResponseWriter, r *http.Request) {
	public := r.PathValue("public") == "public"
	owner := ""
	if !public {
		owner = currentUser(r).ID
	}
	out, err := a.journal.ColumnCover(r.Context(), owner, r.PathValue("id"), r.PathValue("revision"), public)
	if err != nil {
		fail(w, err)
		return
	}
	writeImage(w, out)
}
func (a *API) publicColumnCover(w http.ResponseWriter, r *http.Request) {
	r.SetPathValue("public", "public")
	a.columnCover(w, r)
}
func (a *API) publicColumns(w http.ResponseWriter, r *http.Request) {
	out, err := a.journal.PublicColumns(r.Context(), r.URL.Query().Get("search"), queryPage(r))
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, out)
}
func (a *API) publicColumn(w http.ResponseWriter, r *http.Request) {
	out, err := a.journal.PublicColumn(r.Context(), r.PathValue("id"))
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, out)
}
func (a *API) resources(w http.ResponseWriter, r *http.Request) {
	out, err := a.journal.Resources(r.Context(), r.URL.Query().Get("kind"), r.URL.Query().Get("state"), r.URL.Query().Get("search"), queryPage(r))
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, out)
}
func (a *API) resource(w http.ResponseWriter, r *http.Request) {
	var out any
	var err error
	switch r.PathValue("kind") {
	case "column":
		out, err = a.journal.AdminColumn(r.Context(), r.PathValue("id"))
	case "entry":
		out, err = a.journal.AdminEntry(r.Context(), r.PathValue("id"))
	default:
		err = fault.New("not_found", "资源类型不存在。")
	}
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, out)
}
func (a *API) resourceAction(w http.ResponseWriter, r *http.Request) {
	var input struct {
		Action  string `json:"action"`
		Reason  string `json:"reason"`
		Version int    `json:"version"`
	}
	if !decode(w, r, &input) {
		return
	}
	err := a.journal.ResourceAction(r.Context(), currentUser(r).ID, r.PathValue("kind"), r.PathValue("id"), input.Action, input.Reason, input.Version)
	if err != nil {
		fail(w, err)
		return
	}
	w.WriteHeader(204)
}
func (a *API) moderateColumn(w http.ResponseWriter, r *http.Request) {
	var input struct {
		Action  string `json:"action"`
		Reason  string `json:"reason"`
		Version int    `json:"version"`
	}
	if !decode(w, r, &input) {
		return
	}
	out, err := a.journal.ModerateColumn(r.Context(), currentUser(r).ID, r.PathValue("id"), input.Action, input.Reason, input.Version)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, out)
}
