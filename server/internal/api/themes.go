package api

import (
	"animemo.local/server/internal/plugins"
	"net/http"
)

func (a *API) themeList(w http.ResponseWriter, r *http.Request) {
	themes, err := a.plugins.Themes(r.Context(), currentUser(r).ID)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, themes)
}
func (a *API) themeSelect(w http.ResponseWriter, r *http.Request) {
	var input plugins.ThemeSelection
	if !decode(w, r, &input) {
		return
	}
	if err := a.plugins.SelectTheme(r.Context(), currentUser(r).ID, input); err != nil {
		fail(w, err)
		return
	}
	write(w, 200, map[string]bool{"updated": true})
}
