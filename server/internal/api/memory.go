package api

import "net/http"

func (a *API) revisions(w http.ResponseWriter, r *http.Request) {
	out, err := a.journal.Revisions(r.Context(), currentUser(r).ID, r.PathValue("id"))
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, map[string]any{"items": out})
}
