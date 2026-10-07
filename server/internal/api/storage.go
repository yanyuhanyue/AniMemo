package api

import "net/http"

func (a *API) storageStatus(w http.ResponseWriter, r *http.Request) {
	out, err := a.storage.Status(r.Context())
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, out)
}
func (a *API) setStorage(w http.ResponseWriter, r *http.Request) {
	var input struct {
		Backend string `json:"backend"`
		Version int    `json:"version"`
	}
	if !decode(w, r, &input) {
		return
	}
	out, err := a.storage.SetBackend(r.Context(), currentUser(r).ID, input.Backend, input.Version)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, out)
}
func (a *API) probeStorage(w http.ResponseWriter, r *http.Request) {
	if err := a.storage.Probe(r.Context(), currentUser(r).ID); err != nil {
		fail(w, err)
		return
	}
	write(w, 200, map[string]string{"message": "R2 测试图片已上传、读取校验并删除。"})
}
func (a *API) storageManifest(w http.ResponseWriter, r *http.Request) {
	out, err := a.storage.Manifest(r.Context(), r.PathValue("id"))
	if err != nil {
		fail(w, err)
		return
	}
	w.Header().Set("Content-Disposition", `attachment; filename="animemo-media-migration.json"`)
	write(w, 200, map[string]any{"items": out})
}
