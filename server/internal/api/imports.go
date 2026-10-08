package api

import (
	"io"
	"net/http"

	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/journal"
)

func (a *API) imports(w http.ResponseWriter, r *http.Request) {
	items, err := a.journal.Imports(r.Context(), currentUser(r).ID)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, map[string]any{"items": items})
}
func (a *API) getImport(w http.ResponseWriter, r *http.Request) {
	job, err := a.journal.Import(r.Context(), currentUser(r).ID, r.PathValue("id"))
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, job)
}
func (a *API) newImport(w http.ResponseWriter, r *http.Request) {
	select {
	case a.transferSlots <- struct{}{}:
		defer func() { <-a.transferSlots }()
	default:
		w.Header().Set("Retry-After", "2")
		fail(w, fault.New("media_busy", "正在处理其他数据文件，请稍后重试。"))
		return
	}
	format := r.URL.Query().Get("format")
	limit := int64(journal.MaxImportBytes)
	if format == "csv" {
		limit = journal.MaxCSVBytes
	} else if format == "json" {
		limit = journal.MaxJournalBytes
	} else if format != "zip" {
		fail(w, fault.New("unsupported_media_type", "文件格式无效。"))
		return
	}
	data, err := io.ReadAll(http.MaxBytesReader(w, r.Body, limit))
	if err != nil {
		fail(w, fault.New("import_too_large", "文件超过该格式的大小限制或上传中断。"))
		return
	}
	job, err := a.journal.NewImport(r.Context(), currentUser(r).ID, format, data)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 202, job)
}
func (a *API) importAction(w http.ResponseWriter, r *http.Request) {
	var input struct {
		Action    string                    `json:"action"`
		Selection []journal.ImportSelection `json:"selection"`
	}
	if !decode(w, r, &input) {
		return
	}
	job, err := a.journal.ImportActionSelected(r.Context(), currentUser(r).ID, r.PathValue("id"), input.Action, input.Selection)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, job)
}
func (a *API) backup(w http.ResponseWriter, r *http.Request) {
	select {
	case a.transferSlots <- struct{}{}:
		defer func() { <-a.transferSlots }()
	default:
		w.Header().Set("Retry-After", "2")
		fail(w, fault.New("media_busy", "正在处理其他数据文件，请稍后重试。"))
		return
	}
	data, err := a.journal.Backup(r.Context(), currentUser(r).ID)
	if err != nil {
		fail(w, err)
		return
	}
	w.Header().Set("Content-Type", "application/zip")
	w.Header().Set("Content-Disposition", `attachment; filename="animemo-backup.zip"`)
	w.Write(data)
}
