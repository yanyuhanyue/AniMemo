// Package mediastore moves validated images between PostgreSQL and a private R2 bucket.
package mediastore

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"

	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/media"
	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	smithyhttp "github.com/aws/smithy-go/transport/http"
)

type R2Config struct{ Endpoint, Bucket, AccessKeyID, SecretAccessKey string }
type R2 struct {
	client               *s3.Client
	Endpoint, Bucket, ID string
}

var r2Host = regexp.MustCompile(`^[a-f0-9]{32}\.r2\.cloudflarestorage\.com$`)
var bucketName = regexp.MustCompile(`^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$`)

func NewR2(config R2Config, transport http.RoundTripper) (*R2, error) {
	if config.Endpoint == "" && config.Bucket == "" && config.AccessKeyID == "" && config.SecretAccessKey == "" {
		return nil, nil
	}
	u, err := url.Parse(config.Endpoint)
	if err != nil || u.Scheme != "https" || !r2Host.MatchString(u.Host) || u.User != nil || (u.Path != "" && u.Path != "/") || u.RawQuery != "" || u.Fragment != "" || !bucketName.MatchString(config.Bucket) || config.AccessKeyID == "" || config.SecretAccessKey == "" {
		return nil, errors.New("R2 requires an HTTPS account endpoint, bucket, access key ID and secret access key")
	}
	endpoint := strings.TrimRight(config.Endpoint, "/")
	if transport == nil {
		transport = http.DefaultTransport
	}
	client := s3.New(s3.Options{Region: "auto", BaseEndpoint: aws.String(endpoint), UsePathStyle: true, Credentials: credentials.NewStaticCredentialsProvider(config.AccessKeyID, config.SecretAccessKey, ""), HTTPClient: &http.Client{Transport: transport, Timeout: 15 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}, RetryMaxAttempts: 2, RequestChecksumCalculation: aws.RequestChecksumCalculationWhenRequired, ResponseChecksumValidation: aws.ResponseChecksumValidationWhenRequired})
	sum := sha256.Sum256([]byte(endpoint + "\x00" + config.Bucket))
	return &R2{client: client, Endpoint: endpoint, Bucket: config.Bucket, ID: hex.EncodeToString(sum[:])}, nil
}
func objectFailure() error {
	return fault.New("service_unavailable", "R2 请求未完成，请检查存储配置后重试；原始图片和待处理任务已保留。")
}
func storageIntegrity() error {
	return fault.New("storage_integrity", "存储图片的校验和或大小不匹配，已停止使用这份内容。")
}
func httpStatus(err error) int {
	var response *smithyhttp.ResponseError
	if errors.As(err, &response) {
		return response.HTTPStatusCode()
	}
	return 0
}
func digest(data []byte) string { sum := sha256.Sum256(data); return hex.EncodeToString(sum[:]) }

func (r *R2) Put(ctx context.Context, key, contentType string, data []byte) error {
	_, err := r.client.PutObject(ctx, &s3.PutObjectInput{Bucket: aws.String(r.Bucket), Key: aws.String(key), Body: bytes.NewReader(data), ContentLength: aws.Int64(int64(len(data))), ContentType: aws.String(contentType), CacheControl: aws.String("private, no-store"), IfNoneMatch: aws.String("*"), Metadata: map[string]string{"sha256": digest(data)}})
	if err != nil && httpStatus(err) != 412 {
		return objectFailure()
	}
	// A lost response or existing immutable key is accepted only after reading back exact bytes.
	_, err = r.Get(ctx, key, contentType, digest(data), len(data))
	return err
}
func (r *R2) Get(ctx context.Context, key, contentType, checksum string, size int) ([]byte, error) {
	out, err := r.client.GetObject(ctx, &s3.GetObjectInput{Bucket: aws.String(r.Bucket), Key: aws.String(key)})
	if err != nil {
		return nil, objectFailure()
	}
	defer out.Body.Close()
	if size < 1 || size > media.MaxBytes {
		return nil, storageIntegrity()
	}
	data, err := io.ReadAll(io.LimitReader(out.Body, int64(size)+1))
	if err != nil || len(data) != size || digest(data) != checksum {
		return nil, storageIntegrity()
	}
	if _, err = media.Validate(data, contentType); err != nil {
		return nil, storageIntegrity()
	}
	return data, nil
}
func (r *R2) Delete(ctx context.Context, key string) error {
	_, err := r.client.DeleteObject(ctx, &s3.DeleteObjectInput{Bucket: aws.String(r.Bucket), Key: aws.String(key)})
	if err != nil && httpStatus(err) != 404 {
		return objectFailure()
	}
	return nil
}
