// Package external coordinates provider I/O with journal-owned mutations.
package external

import (
	"context"
	"crypto/cipher"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"strconv"
	"strings"

	"animemo.local/server/internal/bangumi"
	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/journal"
	"animemo.local/server/internal/media"
	"github.com/jackc/pgx/v5/pgxpool"
)

type Service struct {
	pool       *pgxpool.Pool
	journal    *journal.Service
	provider   *bangumi.Client
	oauth      bangumi.OAuthConfig
	encryption cipher.AEAD
}

func New(pool *pgxpool.Pool, provider *bangumi.Client) *Service {
	if provider == nil {
		provider = bangumi.New(nil)
	}
	return &Service{pool: pool, journal: journal.New(pool), provider: provider}
}

type SubjectPreview struct {
	Metadata journal.SourceMetadata `json:"metadata"`
	Snapshot string                 `json:"snapshot"`
	HasCover bool                   `json:"has_cover"`
}
type SearchResult struct {
	Items    []SubjectPreview `json:"items"`
	Total    int              `json:"total"`
	Page     int              `json:"page"`
	PageSize int              `json:"page_size"`
}

func clipped(text string, limit int) string {
	text = strings.TrimSpace(strings.ReplaceAll(text, "\x00", ""))
	runes := []rune(text)
	if len(runes) > limit {
		return string(runes[:limit])
	}
	return text
}
func metadataFor(subject bangumi.Subject) SubjectPreview {
	title := subject.NameCN
	if strings.TrimSpace(title) == "" {
		title = subject.Name
	}
	format := map[string]string{"TV": "tv", "剧场版": "movie", "OVA": "ova", "OAD": "ova"}[subject.Platform]
	if format == "" {
		format = "other"
	}
	studio := ""
	for _, item := range subject.Infobox {
		if item.Key == "动画制作" {
			_ = json.Unmarshal(item.Value, &studio)
			break
		}
	}
	// eps is the work's declared length. total_episodes counts database chapters,
	// including specials and credits, and must not inflate the progress denominator.
	metadata := journal.SourceMetadata{SubjectID: subject.ID, Title: clipped(title, 160), OriginalTitle: clipped(subject.Name, 160), Format: format, TotalEpisodes: max(0, min(subject.Episodes, 10000)), Details: journal.Details{Studio: clipped(studio, 120), AiringPeriod: clipped(subject.Date, 50), Description: clipped(subject.Summary, 8000), ReferenceURL: "https://bgm.tv/subject/" + strconv.FormatInt(subject.ID, 10)}}
	data, _ := json.Marshal(struct {
		Metadata journal.SourceMetadata
		Cover    string
	}{metadata, subject.Images.Large + subject.Images.Common})
	sum := sha256.Sum256(data)
	return SubjectPreview{Metadata: metadata, Snapshot: hex.EncodeToString(sum[:]), HasCover: bangumi.ValidImageURL(subject.Images.Large) || bangumi.ValidImageURL(subject.Images.Common)}
}
func (s *Service) Search(ctx context.Context, query string, page int) (SearchResult, error) {
	results, err := s.provider.Search(ctx, query, page)
	out := SearchResult{Items: []SubjectPreview{}, Total: results.Total, Page: page, PageSize: 12}
	if err != nil {
		return out, err
	}
	for _, subject := range results.Data {
		if subject.Type == 2 && subject.ID > 0 {
			out.Items = append(out.Items, metadataFor(subject))
		}
	}
	return out, nil
}
func (s *Service) Subject(ctx context.Context, subjectID int64) (SubjectPreview, error) {
	subject, err := s.provider.Subject(ctx, subjectID)
	if err != nil {
		return SubjectPreview{}, err
	}
	return metadataFor(subject), nil
}
func (s *Service) Cover(ctx context.Context, subjectID int64) (media.Image, error) {
	subject, err := s.provider.Subject(ctx, subjectID)
	if err != nil {
		return media.Image{}, err
	}
	return s.provider.Cover(ctx, subject)
}

type ApplyInput struct {
	SubjectID int64    `json:"subject_id"`
	Version   int      `json:"version"`
	Snapshot  string   `json:"snapshot"`
	Fields    []string `json:"fields"`
	Cover     bool     `json:"cover"`
}

func (s *Service) Apply(ctx context.Context, owner, entryID string, input ApplyInput) (journal.Entry, error) {
	subject, err := s.provider.Subject(ctx, input.SubjectID)
	if err != nil {
		return journal.Entry{}, err
	}
	preview := metadataFor(subject)
	if preview.Snapshot != input.Snapshot {
		return journal.Entry{}, fault.New("version_conflict", "Bangumi 资料已更新，请重新预览后确认。")
	}
	var cover *media.Image
	if input.Cover {
		picture, err := s.provider.Cover(ctx, subject)
		if err != nil {
			return journal.Entry{}, err
		}
		cover = &picture
	}
	return s.journal.ApplySource(ctx, owner, entryID, input.Version, preview.Metadata, input.Fields, cover)
}
