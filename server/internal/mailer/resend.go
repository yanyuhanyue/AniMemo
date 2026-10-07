// Package mailer sends transactional mail using https://resend.com/docs/api-reference/emails/send-email.
package mailer

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/mail"
	"strconv"
	"strings"
	"time"
)

type Client struct {
	key  string
	From string
	http *http.Client
}
type Message struct {
	From    string   `json:"from"`
	To      []string `json:"to"`
	Subject string   `json:"subject"`
	Text    string   `json:"text"`
}
type DeliveryError struct {
	Retryable  bool
	RetryAfter time.Duration
}

func (*DeliveryError) Error() string { return "transactional email was not accepted" }

func New(key, from string, transport http.RoundTripper) (*Client, error) {
	if key == "" && from == "" {
		return nil, nil
	}
	address, err := mail.ParseAddress(from)
	if key == "" || err != nil || !strings.Contains(address.Address, "@") || strings.ContainsAny(key, "\r\n") {
		return nil, errors.New("RESEND_API_KEY and a valid RESEND_FROM_EMAIL must be configured together")
	}
	if transport == nil {
		transport = http.DefaultTransport
	}
	return &Client{key: key, From: from, http: &http.Client{Transport: transport, Timeout: 10 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}}, nil
}

func (c *Client) Send(ctx context.Context, idempotencyKey string, message Message) (string, error) {
	body, err := json.Marshal(message)
	if err != nil {
		return "", err
	}
	req, err := http.NewRequestWithContext(ctx, "POST", "https://api.resend.com/emails", bytes.NewReader(body))
	if err != nil {
		return "", err
	}
	req.Header.Set("Authorization", "Bearer "+c.key)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Idempotency-Key", idempotencyKey)
	response, err := c.http.Do(req)
	if err != nil {
		return "", &DeliveryError{Retryable: true}
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		delay := time.Second
		if seconds, err := strconv.Atoi(response.Header.Get("Retry-After")); err == nil {
			delay = time.Duration(max(1, min(seconds, 3600))) * time.Second
		} else if when, err := http.ParseTime(response.Header.Get("Retry-After")); err == nil {
			delay = max(time.Second, min(time.Until(when), time.Hour))
		}
		return "", &DeliveryError{Retryable: response.StatusCode == 429 || response.StatusCode >= 500, RetryAfter: delay}
	}
	data, err := io.ReadAll(io.LimitReader(response.Body, 4097))
	if err != nil || len(data) > 4096 {
		return "", &DeliveryError{Retryable: true}
	}
	var out struct {
		ID string `json:"id"`
	}
	if json.Unmarshal(data, &out) != nil || out.ID == "" || len(out.ID) > 128 {
		return "", &DeliveryError{Retryable: true}
	}
	return out.ID, nil
}
