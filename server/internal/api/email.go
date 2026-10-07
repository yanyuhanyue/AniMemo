package api

import (
	"net/http"
	"strings"

	"animemo.local/server/internal/accounts"
)

func (a *API) emailOptions(w http.ResponseWriter, r *http.Request) {
	write(w, 200, map[string]bool{"email_verification": a.accounts.MailEnabled(), "password_reset": a.accounts.MailEnabled()})
}
func (a *API) requestEmail(w http.ResponseWriter, r *http.Request) {
	var input struct {
		Email   string `json:"email"`
		Purpose string `json:"purpose"`
	}
	if !decode(w, r, &input) {
		return
	}
	if err := a.accounts.RequestEmail(r.Context(), input.Email, input.Purpose); err != nil {
		fail(w, err)
		return
	}
	write(w, 202, map[string]string{"message": "如果邮箱符合条件，邮件会稍后送达。请检查收件箱和垃圾邮件，并至少等待一分钟后再申请。"})
}
func (a *API) confirmEmail(w http.ResponseWriter, r *http.Request) {
	var input accounts.EmailConfirmation
	if !decode(w, r, &input) {
		return
	}
	purpose := "verify"
	if strings.HasSuffix(r.URL.Path, "/reset") {
		purpose = "reset"
	}
	if err := a.accounts.ConfirmEmail(r.Context(), purpose, input); err != nil {
		fail(w, err)
		return
	}
	write(w, 200, map[string]string{"message": "密码已设置，其他设备的登录已撤销。请使用新密码登录。"})
}
