package api

import (
	"io"
	"net/http"

	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/media"
)

func (a *API) siteImage(w http.ResponseWriter, r *http.Request) {
	picture, err := a.accounts.SiteImage(r.Context(), r.PathValue("kind"), r.PathValue("revision"))
	if err != nil {
		fail(w, err)
		return
	}
	w.Header().Set("Content-Type", picture.ContentType)
	w.Header().Set("Cross-Origin-Resource-Policy", "same-origin")
	w.Header().Set("Content-Security-Policy", "default-src 'none'; sandbox")
	w.Header().Set("Cache-Control", "public, max-age=86400, immutable")
	w.Write(picture.Data)
}

func (a *API) setSiteImage(w http.ResponseWriter, r *http.Request) {
	version, ok := coverVersion(w, r)
	if !ok {
		return
	}
	var data []byte
	if r.Method == http.MethodPut {
		select {
		case a.coverSlots <- struct{}{}:
			defer func() { <-a.coverSlots }()
		default:
			w.Header().Set("Retry-After", "2")
			fail(w, fault.New("media_busy", "正在处理其他图片，请稍后重试。"))
			return
		}
		var err error
		data, err = io.ReadAll(http.MaxBytesReader(w, r.Body, media.MaxBytes))
		if err != nil {
			fail(w, fault.New("cover_too_large", "图片上传失败或超过 2 MB。"))
			return
		}
	}
	site, err := a.accounts.SetSiteImage(r.Context(), currentUser(r).ID, r.PathValue("kind"), version, r.Header.Get("Content-Type"), data)
	if err != nil {
		fail(w, err)
		return
	}
	write(w, 200, site)
}
