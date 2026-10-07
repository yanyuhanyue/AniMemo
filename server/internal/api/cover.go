package api

import (
	"errors"
	"io"
	"net/http"
	"strconv"

	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/media"
)

func (a *API) cover(w http.ResponseWriter, r *http.Request) {
	cover, err := a.journal.Cover(r.Context(), currentUser(r).ID, r.PathValue("id"), r.PathValue("revision"))
	if err != nil {
		fail(w, err)
		return
	}
	w.Header().Set("Content-Type", cover.ContentType)
	w.Header().Set("Content-Length", strconv.Itoa(len(cover.Data)))
	w.Header().Set("Cross-Origin-Resource-Policy", "same-origin")
	w.Header().Set("Content-Security-Policy", "default-src 'none'; sandbox")
	w.Write(cover.Data)
}

func (a *API) setCover(w http.ResponseWriter, r *http.Request) {
	// Reject inaccessible entries before spending resources on image decoding.
	if _, err := a.journal.Get(r.Context(), currentUser(r).ID, r.PathValue("id")); err != nil {
		fail(w, err)
		return
	}
	version, ok := coverVersion(w, r)
	if !ok {
		return
	}
	select {
	case a.coverSlots <- struct{}{}:
		defer func() { <-a.coverSlots }()
	default:
		w.Header().Set("Retry-After", "2")
		fail(w, fault.New("media_busy", "正在处理其他封面，请稍后重试。"))
		return
	}
	data, err := io.ReadAll(http.MaxBytesReader(w, r.Body, media.MaxBytes))
	if err != nil {
		var tooLarge *http.MaxBytesError
		if errors.As(err, &tooLarge) {
			fail(w, fault.New("cover_too_large", "封面不能超过 2 MB。"))
		} else {
			fail(w, fault.Field("cover", "图片上传中断，请重试。"))
		}
		return
	}
	e, err := a.journal.SetCover(r.Context(), currentUser(r).ID, r.PathValue("id"), version, r.Header.Get("Content-Type"), data)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, http.StatusOK, e)
}

func (a *API) deleteCover(w http.ResponseWriter, r *http.Request) {
	version, ok := coverVersion(w, r)
	if !ok {
		return
	}
	e, err := a.journal.DeleteCover(r.Context(), currentUser(r).ID, r.PathValue("id"), version)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, http.StatusOK, e)
}

func coverVersion(w http.ResponseWriter, r *http.Request) (int, bool) {
	version, err := strconv.Atoi(r.URL.Query().Get("version"))
	if err != nil || version < 1 {
		fail(w, fault.Field("version", "缺少记录版本，请刷新后重试。"))
		return 0, false
	}
	return version, true
}
