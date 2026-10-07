// Package bangumi implements the documented API at https://github.com/bangumi/api.
// Destinations are fixed here; neither browser input nor API responses can choose an API host.
package bangumi

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/media"
)

const apiOrigin = "https://api.bgm.tv"
const authOrigin = "https://bgm.tv"
const UserAgent = "AniMemo/0.1.0 (+https://github.com/yanyuhanyue/AniMemo)"

type Client struct {
	http     *http.Client
	mu       sync.Mutex
	next     time.Time
	blocked  time.Time
	interval time.Duration
}

// New accepts a transport for controlled tests. Production passes nil and keeps TLS verification.
func New(transport http.RoundTripper) *Client {
	if transport == nil {
		transport = http.DefaultTransport
	}
	return &Client{http: &http.Client{Transport: transport, Timeout: 12 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}, interval: 500 * time.Millisecond}
}

type UpstreamError struct {
	Status     int
	RetryAfter time.Duration
}

func (e *UpstreamError) Error() string { return "Bangumi upstream request failed" }
func (e *UpstreamError) Unwrap() error {
	switch e.Status {
	case 404:
		return fault.New("not_found", "Bangumi 没有返回这项内容；它也可能需要授权才能查看。")
	case 401, 403:
		return fault.New("forbidden", "Bangumi 授权已失效或权限不足，请重新连接账号。")
	case 429:
		return fault.New("rate_limited", "Bangumi 请求较多，请稍后重试。")
	default:
		return fault.New("service_unavailable", "暂时无法连接 Bangumi，请稍后重试。")
	}
}
func Retryable(err error) bool {
	var e *UpstreamError
	return errors.As(err, &e) && (e.Status == 0 || e.Status == 429 || e.Status >= 500)
}

func (c *Client) wait(ctx context.Context) error {
	c.mu.Lock()
	now := time.Now()
	if c.blocked.After(now) {
		delay := c.blocked.Sub(now)
		c.mu.Unlock()
		return &UpstreamError{Status: 429, RetryAfter: delay}
	}
	next := c.next
	if next.Before(now) {
		next = now
	}
	// Bound the queue as well as requests in flight.
	if next.Sub(now) > 2*time.Second {
		c.mu.Unlock()
		return &UpstreamError{Status: 429, RetryAfter: next.Sub(now)}
	}
	c.next = next.Add(c.interval)
	c.mu.Unlock()
	timer := time.NewTimer(time.Until(next))
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

func (c *Client) request(ctx context.Context, method, address, token, kind string, body []byte, maxBytes int64) ([]byte, string, error) {
	if err := c.wait(ctx); err != nil {
		return nil, "", err
	}
	req, err := http.NewRequestWithContext(ctx, method, address, bytes.NewReader(body))
	if err != nil {
		return nil, "", &UpstreamError{}
	}
	req.Header.Set("User-Agent", UserAgent)
	req.Header.Set("Accept", "application/json")
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	if kind != "" {
		req.Header.Set("Content-Type", kind)
	}
	response, err := c.http.Do(req)
	if err != nil {
		if ctx.Err() != nil {
			return nil, "", ctx.Err()
		}
		return nil, "", &UpstreamError{}
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		delay := time.Second
		if seconds, err := strconv.Atoi(response.Header.Get("Retry-After")); err == nil && seconds >= 0 {
			delay = time.Duration(min(seconds, 3600)) * time.Second
		} else if when, err := http.ParseTime(response.Header.Get("Retry-After")); err == nil {
			delay = max(time.Second, min(time.Until(when), time.Hour))
		}
		if response.StatusCode == 429 {
			c.mu.Lock()
			c.blocked = time.Now().Add(delay)
			c.mu.Unlock()
		}
		return nil, "", &UpstreamError{Status: response.StatusCode, RetryAfter: delay}
	}
	data, err := io.ReadAll(io.LimitReader(response.Body, maxBytes+1))
	if err != nil || int64(len(data)) > maxBytes {
		return nil, "", fault.New("service_unavailable", "Bangumi 返回的内容无效或超过大小限制。")
	}
	return data, response.Header.Get("Content-Type"), nil
}

func (c *Client) json(ctx context.Context, method, path, token string, payload, target any) error {
	var body []byte
	var err error
	if payload != nil {
		body, err = json.Marshal(payload)
		if err != nil {
			return err
		}
	}
	data, _, err := c.request(ctx, method, apiOrigin+path, token, "application/json", body, 2<<20)
	if err != nil {
		return err
	}
	if target == nil {
		return nil
	}
	if err = json.Unmarshal(data, target); err != nil {
		return fault.New("service_unavailable", "Bangumi 返回的资料格式无效。")
	}
	return nil
}

type Subject struct {
	ID            int64  `json:"id"`
	Type          int    `json:"type"`
	Name          string `json:"name"`
	NameCN        string `json:"name_cn"`
	Summary       string `json:"summary"`
	Date          string `json:"date"`
	Platform      string `json:"platform"`
	Episodes      int    `json:"eps"`
	TotalEpisodes int    `json:"total_episodes"`
	Images        struct {
		Common string `json:"common"`
		Large  string `json:"large"`
	} `json:"images"`
	Infobox []struct {
		Key   string          `json:"key"`
		Value json.RawMessage `json:"value"`
	} `json:"infobox"`
}
type SearchPage struct {
	Data   []Subject `json:"data"`
	Total  int       `json:"total"`
	Limit  int       `json:"limit"`
	Offset int       `json:"offset"`
}

func (c *Client) Search(ctx context.Context, query string, page int) (SearchPage, error) {
	query = strings.TrimSpace(query)
	if utf8.RuneCountInString(query) < 1 || utf8.RuneCountInString(query) > 160 || strings.ContainsRune(query, 0) || page < 1 || page > 50 {
		return SearchPage{}, fault.Field("query", "请输入 1–160 个字的搜索词，页码在 1–50 之间。")
	}
	out := SearchPage{Data: []Subject{}}
	err := c.json(ctx, "POST", "/v0/search/subjects?limit=12&offset="+strconv.Itoa((page-1)*12), "", map[string]any{"keyword": query, "sort": "match", "filter": map[string]any{"type": []int{2}, "nsfw": false}}, &out)
	return out, err
}
func (c *Client) Subject(ctx context.Context, subject int64) (Subject, error) {
	var out Subject
	if subject < 1 || subject > 2147483647 {
		return out, fault.Field("subject_id", "Bangumi 条目标识无效。")
	}
	err := c.json(ctx, "GET", "/v0/subjects/"+strconv.FormatInt(subject, 10), "", nil, &out)
	if err == nil && (out.ID != subject || out.Type != 2) {
		err = fault.New("validation_error", "请选择 Bangumi 中的动画条目。")
	}
	return out, err
}
func ValidImageURL(address string) bool {
	u, err := url.Parse(address)
	return err == nil && u.Scheme == "https" && u.Host == "lain.bgm.tv" && u.User == nil && u.Fragment == "" && len(address) <= 2000 && (strings.HasPrefix(u.Path, "/pic/cover/") || strings.HasPrefix(u.Path, "/r/"))
}
func (c *Client) Cover(ctx context.Context, subject Subject) (media.Image, error) {
	address := subject.Images.Large
	if address == "" {
		address = subject.Images.Common
	}
	if !ValidImageURL(address) {
		return media.Image{}, fault.New("unsupported_media_type", "资料源没有提供受支持的封面地址。")
	}
	data, kind, err := c.request(ctx, "GET", address, "", "", nil, media.MaxBytes)
	if err != nil {
		return media.Image{}, err
	}
	return media.Validate(data, kind)
}

type OAuthConfig struct{ ClientID, ClientSecret, RedirectURI string }

func (o OAuthConfig) Enabled() bool {
	return o.ClientID != "" && o.ClientSecret != "" && o.RedirectURI != ""
}
func (o OAuthConfig) AuthorizeURL(state string) string {
	return authOrigin + "/oauth/authorize?" + url.Values{"client_id": {o.ClientID}, "response_type": {"code"}, "redirect_uri": {o.RedirectURI}, "state": {state}}.Encode()
}

type Token struct {
	AccessToken  string `json:"access_token"`
	RefreshToken string `json:"refresh_token"`
	ExpiresIn    int    `json:"expires_in"`
	UserID       int64  `json:"user_id"`
	TokenType    string `json:"token_type"`
}

func (c *Client) Exchange(ctx context.Context, config OAuthConfig, grant, value string) (Token, error) {
	var out Token
	form := url.Values{"client_id": {config.ClientID}, "client_secret": {config.ClientSecret}, "redirect_uri": {config.RedirectURI}, "grant_type": {grant}}
	if grant == "authorization_code" {
		form.Set("code", value)
	} else if grant == "refresh_token" {
		form.Set("refresh_token", value)
	} else {
		return out, fault.New("validation_error", "授权方式无效。")
	}
	data, _, err := c.request(ctx, "POST", authOrigin+"/oauth/access_token", "", "application/x-www-form-urlencoded", []byte(form.Encode()), 64<<10)
	if err != nil {
		return out, err
	}
	if json.Unmarshal(data, &out) != nil || out.AccessToken == "" || out.RefreshToken == "" || out.ExpiresIn < 60 || out.ExpiresIn > 366*24*3600 || !strings.EqualFold(out.TokenType, "Bearer") {
		return Token{}, fault.New("service_unavailable", "Bangumi 返回的授权结果无效。")
	}
	return out, nil
}

type User struct {
	ID       int64  `json:"id"`
	Username string `json:"username"`
	Nickname string `json:"nickname"`
}

func (c *Client) Me(ctx context.Context, token string) (User, error) {
	var out User
	err := c.json(ctx, "GET", "/v0/me", token, nil, &out)
	if err == nil && (out.ID < 1 || out.Username == "") {
		err = fault.New("service_unavailable", "Bangumi 返回的用户身份无效。")
	}
	return out, err
}

type Collection struct {
	SubjectID   int64    `json:"subject_id"`
	SubjectType int      `json:"subject_type"`
	Type        int      `json:"type"`
	Rate        int      `json:"rate"`
	Comment     string   `json:"comment"`
	Tags        []string `json:"tags"`
	Episodes    int      `json:"ep_status"`
	Private     bool     `json:"private"`
	Subject     Subject  `json:"subject"`
}
type CollectionPage struct {
	Data   []Collection `json:"data"`
	Total  int          `json:"total"`
	Offset int          `json:"offset"`
	Limit  int          `json:"limit"`
}

func (c *Client) Collections(ctx context.Context, user int64, token string, offset int) (CollectionPage, error) {
	out := CollectionPage{Data: []Collection{}}
	err := c.json(ctx, "GET", "/v0/users/"+strconv.FormatInt(user, 10)+"/collections?subject_type=2&limit=50&offset="+strconv.Itoa(offset), token, nil, &out)
	return out, err
}
func (c *Client) Collection(ctx context.Context, user, subject int64, token string) (*Collection, error) {
	var out Collection
	err := c.json(ctx, "GET", "/v0/users/"+strconv.FormatInt(user, 10)+"/collections/"+strconv.FormatInt(subject, 10), token, nil, &out)
	var upstream *UpstreamError
	if errors.As(err, &upstream) && upstream.Status == 404 {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &out, nil
}

type CollectionWrite struct {
	Type    int      `json:"type"`
	Rate    int      `json:"rate"`
	Comment string   `json:"comment"`
	Tags    []string `json:"tags"`
	Private bool     `json:"private"`
}

type Episode struct {
	ID   int64   `json:"id"`
	Sort float64 `json:"sort"`
	Type int     `json:"type"`
}
type EpisodeCollection struct {
	Episode Episode `json:"episode"`
	Type    int     `json:"type"`
}

func (c *Client) Episodes(ctx context.Context, subject int64, token string) ([]EpisodeCollection, error) {
	var page struct {
		Total int                 `json:"total"`
		Data  []EpisodeCollection `json:"data"`
	}
	err := c.json(ctx, "GET", "/v0/users/-/collections/"+strconv.FormatInt(subject, 10)+"/episodes?episode_type=0&limit=1000&offset=0", token, nil, &page)
	if err != nil {
		return nil, err
	}
	if page.Total > 1000 || page.Total != len(page.Data) {
		return nil, fault.New("validation_error", "逐话同步只支持完整提供且不超过 1000 话的正片目录。")
	}
	return page.Data, nil
}
func (c *Client) EpisodeCatalog(ctx context.Context, subject int64) ([]EpisodeCollection, error) {
	var page struct {
		Total int       `json:"total"`
		Data  []Episode `json:"data"`
	}
	err := c.json(ctx, "GET", "/v0/episodes?subject_id="+strconv.FormatInt(subject, 10)+"&type=0&limit=1000&offset=0", "", nil, &page)
	if err != nil {
		return nil, err
	}
	if page.Total > 1000 || page.Total != len(page.Data) {
		return nil, fault.New("validation_error", "逐话同步只支持完整提供且不超过 1000 话的正片目录。")
	}
	out := []EpisodeCollection{}
	for _, ep := range page.Data {
		out = append(out, EpisodeCollection{Episode: ep, Type: 0})
	}
	return out, nil
}
func (c *Client) WriteEpisodes(ctx context.Context, subject int64, token string, ids []int64, state int) error {
	if len(ids) == 0 {
		return nil
	}
	return c.json(ctx, "PATCH", "/v0/users/-/collections/"+strconv.FormatInt(subject, 10)+"/episodes", token, map[string]any{"episode_id": ids, "type": state}, nil)
}

func (c *Client) WriteCollection(ctx context.Context, subject int64, token string, payload CollectionWrite) error {
	return c.json(ctx, "POST", "/v0/users/-/collections/"+strconv.FormatInt(subject, 10), token, payload, nil)
}
