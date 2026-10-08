package api

import (
	"animemo.local/server/internal/journal"
	"net/http"
)

func (a *API) memorySharingRoutes(mux *http.ServeMux) {
	mux.HandleFunc("POST /api/v1/memory/shares", a.require(func(w http.ResponseWriter, r *http.Request) {
		var in journal.MemoryShareInput
		if !decode(w, r, &in) {
			return
		}
		out, err := a.journal.MemoryShare(r.Context(), currentUser(r).ID, in)
		libraryReply(w, 200, out, err)
	}))
	for _, route := range []string{"/api/v1/memory/shared/{token}", "/api/v1/memory/public/{kind}/{id}"} {
		mux.HandleFunc("GET "+route, func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("X-Robots-Tag", "noindex, nofollow")
			out, err := a.journal.SharedMemory(r.Context(), r.PathValue("token"), r.PathValue("kind"), r.PathValue("id"))
			libraryReply(w, 200, out, err)
		})
		mux.HandleFunc("GET "+route+"/media/{media}", func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("X-Robots-Tag", "noindex, nofollow")
			out, err := a.journal.SharedMemoryMedia(r.Context(), r.PathValue("token"), r.PathValue("kind"), r.PathValue("id"), r.PathValue("media"))
			if err != nil {
				fail(w, err)
				return
			}
			w.Header().Set("Content-Type", out.ContentType)
			w.Header().Set("Cross-Origin-Resource-Policy", "same-origin")
			w.Write(out.Data)
		})
	}
	mux.HandleFunc("GET /api/v1/homepage", func(w http.ResponseWriter, r *http.Request) {
		out, err := a.journal.SiteHomepage(r.Context())
		libraryReply(w, 200, out, err)
	})
}
