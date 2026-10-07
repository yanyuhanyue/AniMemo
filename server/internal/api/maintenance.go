package api

import (
	"animemo.local/server/internal/journal"
	"net/http"
)

func (a *API) presets(w http.ResponseWriter, r *http.Request) {
	items, err := a.journal.Presets(r.Context())
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, map[string]any{"items": items})
}
func (a *API) savePreset(w http.ResponseWriter, r *http.Request) {
	var input journal.Preset
	if !decode(w, r, &input) {
		return
	}
	err := a.journal.SavePreset(r.Context(), currentUser(r).ID, input, r.Method == "DELETE")
	if err != nil {
		fail(w, err)
		return
	}
	w.WriteHeader(204)
}
func (a *API) instanceStatus(w http.ResponseWriter, r *http.Request) {
	out, err := a.accounts.InstanceStatus(r.Context())
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, out)
}
func (a *API) maintenance(w http.ResponseWriter, r *http.Request) {
	var input struct {
		Action string `json:"action"`
	}
	if !decode(w, r, &input) {
		return
	}
	out, err := a.accounts.Maintenance(r.Context(), currentUser(r).ID, input.Action)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, out)
}
