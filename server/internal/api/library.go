package api

import (
	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/journal"
	"animemo.local/server/internal/media"
	"context"
	"io"
	"net/http"
	"strconv"
)

func libraryFilter(r *http.Request) journal.LibraryFilter {
	q := r.URL.Query()
	return journal.LibraryFilter{Search: q.Get("search"), AnimeID: q.Get("anime_id"), CharacterID: q.Get("character_id"), Kind: q.Get("kind"), Year: q.Get("year"), Highlight: q.Get("highlight") == "true", Page: queryPage(r)}
}
func libraryReply(w http.ResponseWriter, status int, out any, err error) {
	if err != nil {
		fail(w, err)
		return
	}
	write(w, status, out)
}
func librarySave[I, O any](save func(context.Context, string, string, I) (O, error)) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var in I
		if !decode(w, r, &in) {
			return
		}
		out, err := save(r.Context(), currentUser(r).ID, r.PathValue("id"), in)
		status := 200
		if r.Method == "POST" {
			status = 201
		}
		libraryReply(w, status, out, err)
	}
}
func (a *API) libraryRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/v1/memory/search", a.require(func(w http.ResponseWriter, r *http.Request) {
		out, err := a.journal.SearchMemory(r.Context(), currentUser(r).ID, journal.MemorySearchFilter{LibraryFilter: libraryFilter(r), Status: r.URL.Query().Get("status")})
		libraryReply(w, 200, out, err)
	}))
	mux.HandleFunc("POST /api/v1/memory/references", a.require(func(w http.ResponseWriter, r *http.Request) {
		var in struct {
			Items []journal.CollectionItem `json:"items"`
		}
		if !decode(w, r, &in) {
			return
		}
		out, err := a.journal.MemoryReferences(r.Context(), currentUser(r).ID, in.Items)
		libraryReply(w, 200, map[string]any{"items": out}, err)
	}))
	mux.HandleFunc("POST /api/v1/memory/episodes/batch", a.require(func(w http.ResponseWriter, r *http.Request) {
		var in journal.EpisodeBatchInput
		if !decode(w, r, &in) {
			return
		}
		out, err := a.journal.CreateEpisodeBatch(r.Context(), currentUser(r).ID, in)
		libraryReply(w, 201, map[string]any{"items": out}, err)
	}))

	mux.HandleFunc("GET /api/v1/memory/notes", a.require(func(w http.ResponseWriter, r *http.Request) {
		out, err := a.journal.Notes(r.Context(), currentUser(r).ID, libraryFilter(r))
		libraryReply(w, 200, out, err)
	}))
	mux.HandleFunc("POST /api/v1/memory/notes", a.require(librarySave(a.journal.SaveNote)))
	mux.HandleFunc("GET /api/v1/memory/notes/{id}", a.require(func(w http.ResponseWriter, r *http.Request) {
		out, err := a.journal.Note(r.Context(), currentUser(r).ID, r.PathValue("id"))
		libraryReply(w, 200, out, err)
	}))
	mux.HandleFunc("PUT /api/v1/memory/notes/{id}", a.require(librarySave(a.journal.SaveNote)))
	mux.HandleFunc("DELETE /api/v1/memory/notes/{id}", a.require(func(w http.ResponseWriter, r *http.Request) {
		v, _ := strconv.Atoi(r.URL.Query().Get("version"))
		err := a.journal.DeleteNote(r.Context(), currentUser(r).ID, r.PathValue("id"), v)
		if err != nil {
			fail(w, err)
			return
		}
		w.WriteHeader(204)
	}))
	mux.HandleFunc("GET /api/v1/memory/revisions/{id}", a.require(func(w http.ResponseWriter, r *http.Request) {
		out, err := a.journal.LibraryRevisions(r.Context(), currentUser(r).ID, r.PathValue("id"))
		libraryReply(w, 200, map[string]any{"items": out}, err)
	}))
	mux.HandleFunc("GET /api/v1/memory/characters", a.require(func(w http.ResponseWriter, r *http.Request) {
		out, err := a.journal.Characters(r.Context(), currentUser(r).ID, libraryFilter(r))
		libraryReply(w, 200, out, err)
	}))
	mux.HandleFunc("POST /api/v1/memory/characters", a.require(librarySave(a.journal.SaveCharacter)))
	mux.HandleFunc("PUT /api/v1/memory/characters/{id}", a.require(librarySave(a.journal.SaveCharacter)))
	mux.HandleFunc("GET /api/v1/memory/characters/{id}", a.require(func(w http.ResponseWriter, r *http.Request) {
		out, err := a.journal.Character(r.Context(), currentUser(r).ID, r.PathValue("id"))
		libraryReply(w, 200, out, err)
	}))
	mux.HandleFunc("POST /api/v1/memory/identities/{kind}/{id}", a.require(func(w http.ResponseWriter, r *http.Request) {
		var in struct {
			Version  int    `json:"version"`
			TargetID string `json:"target_id"`
		}
		if !decode(w, r, &in) {
			return
		}
		err := a.journal.RedirectIdentity(r.Context(), currentUser(r).ID, r.PathValue("kind"), r.PathValue("id"), in.TargetID, in.Version)
		if err != nil {
			fail(w, err)
			return
		}
		w.WriteHeader(204)
	}))
	mux.HandleFunc("GET /api/v1/memory/episodes", a.require(func(w http.ResponseWriter, r *http.Request) {
		out, err := a.journal.Episodes(r.Context(), currentUser(r).ID, libraryFilter(r))
		libraryReply(w, 200, out, err)
	}))
	mux.HandleFunc("POST /api/v1/memory/episodes", a.require(librarySave(a.journal.SaveEpisode)))
	mux.HandleFunc("PUT /api/v1/memory/episodes/{id}", a.require(librarySave(a.journal.SaveEpisode)))
	mux.HandleFunc("GET /api/v1/memory/relations", a.require(func(w http.ResponseWriter, r *http.Request) {
		out, err := a.journal.Relations(r.Context(), currentUser(r).ID)
		libraryReply(w, 200, map[string]any{"items": out}, err)
	}))
	for _, method := range []string{"PUT", "DELETE"} {
		mux.HandleFunc(method+" /api/v1/memory/relations", a.require(func(w http.ResponseWriter, r *http.Request) {
			var in journal.AnimeRelation
			if !decode(w, r, &in) {
				return
			}
			err := a.journal.SetRelation(r.Context(), currentUser(r).ID, in, r.Method == "DELETE")
			if err != nil {
				fail(w, err)
				return
			}
			w.WriteHeader(204)
		}))
	}
	mux.HandleFunc("POST /api/v1/memory/progress", a.require(func(w http.ResponseWriter, r *http.Request) {
		var in journal.ProgressAssertionInput
		if !decode(w, r, &in) {
			return
		}
		out, err := a.journal.AssertProgress(r.Context(), currentUser(r).ID, in)
		libraryReply(w, 201, out, err)
	}))
	mux.HandleFunc("GET /api/v1/memory/progress", a.require(func(w http.ResponseWriter, r *http.Request) {
		out, err := a.journal.ProgressAssertions(r.Context(), currentUser(r).ID, r.URL.Query().Get("anime_id"))
		libraryReply(w, 200, map[string]any{"items": out}, err)
	}))
	mux.HandleFunc("POST /api/v1/memory/media", a.require(func(w http.ResponseWriter, r *http.Request) {
		var in struct {
			ByteSize int `json:"byte_size"`
		}
		if !decode(w, r, &in) {
			return
		}
		out, err := a.journal.ReserveMemoryMedia(r.Context(), currentUser(r).ID, in.ByteSize)
		libraryReply(w, 201, out, err)
	}))
	mux.HandleFunc("GET /api/v1/memory/media", a.require(func(w http.ResponseWriter, r *http.Request) {
		out, err := a.journal.MemoryMedia(r.Context(), currentUser(r).ID, queryPage(r))
		libraryReply(w, 200, out, err)
	}))
	mux.HandleFunc("PUT /api/v1/memory/media/{id}", a.require(a.uploadMemoryMedia))
	mux.HandleFunc("GET /api/v1/memory/media/{id}", a.require(func(w http.ResponseWriter, r *http.Request) {
		out, err := a.journal.ReadMemoryMedia(r.Context(), currentUser(r).ID, r.PathValue("id"), r.URL.Query().Get("thumbnail") == "true")
		if err != nil {
			fail(w, err)
			return
		}
		w.Header().Set("Content-Type", out.ContentType)
		w.Header().Set("Cross-Origin-Resource-Policy", "same-origin")
		w.Header().Set("Content-Security-Policy", "default-src 'none'; sandbox")
		w.Write(out.Data)
	}))
	mux.HandleFunc("DELETE /api/v1/memory/media/{id}", a.require(func(w http.ResponseWriter, r *http.Request) {
		if err := a.journal.DeleteMemoryMedia(r.Context(), currentUser(r).ID, r.PathValue("id")); err != nil {
			fail(w, err)
			return
		}
		w.WriteHeader(204)
	}))
	mux.HandleFunc("GET /api/v1/memory/collections", a.require(func(w http.ResponseWriter, r *http.Request) {
		out, err := a.journal.Collections(r.Context(), currentUser(r).ID)
		libraryReply(w, 200, map[string]any{"items": out}, err)
	}))
	mux.HandleFunc("POST /api/v1/memory/collections", a.require(librarySave(a.journal.SaveCollection)))
	mux.HandleFunc("PUT /api/v1/memory/collections/{id}", a.require(librarySave(a.journal.SaveCollection)))
	mux.HandleFunc("DELETE /api/v1/memory/collections/{id}", a.require(func(w http.ResponseWriter, r *http.Request) {
		v, _ := strconv.Atoi(r.URL.Query().Get("version"))
		if err := a.journal.DeleteCollection(r.Context(), currentUser(r).ID, r.PathValue("id"), v); err != nil {
			fail(w, err)
			return
		}
		w.WriteHeader(204)
	}))
	mux.HandleFunc("GET /api/v1/memory/yearly", a.require(func(w http.ResponseWriter, r *http.Request) {
		out, err := a.journal.Yearlies(r.Context(), currentUser(r).ID)
		libraryReply(w, 200, map[string]any{"items": out}, err)
	}))
	mux.HandleFunc("GET /api/v1/memory/yearly/{id}", a.require(func(w http.ResponseWriter, r *http.Request) {
		out, err := a.journal.Yearly(r.Context(), currentUser(r).ID, r.PathValue("id"))
		libraryReply(w, 200, out, err)
	}))
	mux.HandleFunc("POST /api/v1/memory/yearly", a.require(librarySave(a.journal.SaveYearly)))
	mux.HandleFunc("PUT /api/v1/memory/yearly/{id}", a.require(librarySave(a.journal.SaveYearly)))
}
func (a *API) uploadMemoryMedia(w http.ResponseWriter, r *http.Request) {
	select {
	case a.coverSlots <- struct{}{}:
		defer func() { <-a.coverSlots }()
	default:
		fail(w, fault.New("media_busy", "正在处理图片，请稍后重试。"))
		return
	}
	data, err := io.ReadAll(http.MaxBytesReader(w, r.Body, media.MaxBytes))
	if err != nil {
		fail(w, fault.New("cover_too_large", "图片上传中断或超过 2 MiB。"))
		return
	}
	out, err := a.journal.FinalizeMemoryMedia(r.Context(), currentUser(r).ID, r.PathValue("id"), r.Header.Get("Content-Type"), data)
	libraryReply(w, 200, out, err)
}
