package journal

import (
	"net/url"
	"strings"
	"unicode/utf8"

	"animemo.local/server/internal/fault"
)

type Details struct {
	Studio       string `json:"studio"`
	AiringPeriod string `json:"airing_period"`
	Description  string `json:"description"`
	ReferenceURL string `json:"reference_url"`
}

func (d *Details) Validate() error {
	d.Studio, d.AiringPeriod = strings.TrimSpace(d.Studio), strings.TrimSpace(d.AiringPeriod)
	d.Description, d.ReferenceURL = strings.TrimSpace(d.Description), strings.TrimSpace(d.ReferenceURL)
	if utf8.RuneCountInString(d.Studio) > 120 || utf8.RuneCountInString(d.AiringPeriod) > 50 || utf8.RuneCountInString(d.Description) > 8000 {
		return fault.Field("details", "制作公司最多 120 字，播出时期最多 50 字，简介最多 8000 字。")
	}
	if d.ReferenceURL != "" {
		u, err := url.Parse(d.ReferenceURL)
		if err != nil || u.Scheme != "https" || u.Hostname() == "" || u.User != nil || len(d.ReferenceURL) > 1000 {
			return fault.Field("reference_url", "资料链接必须是有效的 HTTPS 地址。")
		}
	}
	return nil
}
