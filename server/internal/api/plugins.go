package api

import (
	"io"
	"net/http"

	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/plugins"
	"animemo.local/server/pkg/pluginproto"
)

func (a *API) pluginList(w http.ResponseWriter, r *http.Request) {
	items, err := a.plugins.List(r.Context(), r.URL.Path == "/api/v1/admin/plugins")
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, map[string]any{"items": items})
}
func (a *API) pluginInstall(w http.ResponseWriter, r *http.Request) {
	data, err := io.ReadAll(http.MaxBytesReader(w, r.Body, plugins.MaxPackageBytes))
	if err != nil {
		fail(w, fault.New("import_too_large", "插件包超过 12 MiB 或上传中断。"))
		return
	}
	if err := a.plugins.Install(r.Context(), currentUser(r).ID, data); err != nil {
		fail(w, err)
		return
	}
	write(w, 201, map[string]bool{"installed": true})
}
func (a *API) pluginChange(w http.ResponseWriter, r *http.Request) {
	var input plugins.Action
	if !decode(w, r, &input) {
		return
	}
	if err := a.plugins.Change(r.Context(), currentUser(r).ID, r.PathValue("slug"), input); err != nil {
		fail(w, err)
		return
	}
	write(w, 200, map[string]bool{"updated": true})
}
func (a *API) pluginImport(w http.ResponseWriter, r *http.Request) {
	data, err := io.ReadAll(http.MaxBytesReader(w, r.Body, plugins.MaxInputBytes))
	if err != nil {
		fail(w, fault.New("import_too_large", "文本文件超过 2 MiB 或上传中断。"))
		return
	}
	out, err := a.plugins.Convert(r.Context(), currentUser(r).ID, r.PathValue("slug"), pluginproto.Request{Protocol: pluginproto.Version, Filename: r.URL.Query().Get("filename"), Text: string(data)})
	if err != nil {
		fail(w, err)
		return
	}
	// A conversion only creates a normal user-owned preview. The existing journal
	// worker remains the sole owner of validation, deduplication and atomic writes.
	job, err := a.journal.NewImport(r.Context(), currentUser(r).ID, out.Format, []byte(out.Data))
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 202, job)
}
