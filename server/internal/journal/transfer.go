package journal

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/csv"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path"
	"strconv"
	"strings"
	"unicode/utf8"

	"animemo.local/server/internal/fault"
	"animemo.local/server/internal/id"
	"animemo.local/server/internal/media"
)

const MaxImportBytes = 160 * 1024 * 1024
const MaxJournalBytes = 64 * 1024 * 1024
const MaxCSVBytes = 2 * 1024 * 1024

type ImportItem struct {
	Entry     Entry         `json:"entry"`
	History   []RecordInput `json:"history"`
	CoverPath string        `json:"cover_path,omitempty"`
}
type ImportDocument struct {
	Items []ImportItem `json:"items"`
}
type ImportPreview struct {
	Total      int                   `json:"total"`
	Ready      int                   `json:"ready"`
	Duplicates int                   `json:"duplicates"`
	Records    int                   `json:"records"`
	Covers     int                   `json:"covers"`
	Warnings   []string              `json:"warnings"`
	Titles     []string              `json:"titles"`
	History    []ImportRecordPreview `json:"history"`
}
type ImportRecordPreview struct {
	Title       string `json:"title"`
	WatchedOn   string `json:"watched_on"`
	EpisodeFrom int    `json:"episode_from"`
	EpisodeTo   int    `json:"episode_to"`
	Rewatch     int    `json:"rewatch"`
	Note        string `json:"note"`
}
type BundleManifest struct {
	Schema string            `json:"schema"`
	Files  map[string]string `json:"files"`
}

func decodeTransfer(data []byte, target any) error {
	decoder := json.NewDecoder(bytes.NewReader(bytes.TrimPrefix(data, []byte{0xef, 0xbb, 0xbf})))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return fault.New("validation_error", "JSON 文件格式或字段无效。")
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		return fault.New("validation_error", "JSON 文件包含多余内容。")
	}
	return nil
}

func readBundle(ctx context.Context, data []byte) (map[string][]byte, error) {
	reader, err := zip.NewReader(bytes.NewReader(data), int64(len(data)))
	if err != nil {
		return nil, fault.New("validation_error", "备份 ZIP 文件无效。")
	}
	if len(reader.File) > 5002 {
		return nil, fault.New("validation_error", "备份文件数量超过限制。")
	}
	files := map[string][]byte{}
	var total int64
	for _, file := range reader.File {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		name := file.Name
		if name == "" || path.Clean(name) != name || strings.Contains(name, "\\") || strings.HasPrefix(name, "/") || strings.Contains(name, "..") || file.FileInfo().IsDir() || file.Mode()&os.ModeType != 0 {
			return nil, fault.New("validation_error", "备份中包含不允许的路径或文件类型。")
		}
		limit := int64(media.MaxBytes)
		if name == "journal.json" {
			limit = MaxJournalBytes
		} else if name == "manifest.json" {
			limit = 2 * 1024 * 1024
		} else if !strings.HasPrefix(name, "covers/") {
			return nil, fault.New("validation_error", "备份中包含未知文件。")
		}
		if _, exists := files[name]; exists || file.UncompressedSize64 > uint64(limit) {
			return nil, fault.New("validation_error", "备份文件重复或超过大小限制。")
		}
		total += int64(file.UncompressedSize64)
		if total > MaxImportBytes {
			return nil, fault.New("validation_error", "备份解压后超过 160 MiB。")
		}
		stream, err := file.Open()
		if err != nil {
			return nil, err
		}
		content, readErr := io.ReadAll(io.LimitReader(stream, limit+1))
		stream.Close()
		if readErr != nil || int64(len(content)) > limit {
			return nil, fault.New("validation_error", "备份内容损坏或超过大小限制。")
		}
		files[name] = content
	}
	var manifest BundleManifest
	if err = decodeTransfer(files["manifest.json"], &manifest); err != nil {
		return nil, err
	}
	if manifest.Schema != "animemo.backup/v1" || len(manifest.Files) != len(files)-1 {
		return nil, fault.New("validation_error", "备份清单无效。")
	}
	for name, expected := range manifest.Files {
		content, exists := files[name]
		sum := sha256.Sum256(content)
		if name == "manifest.json" || !exists || hex.EncodeToString(sum[:]) != expected {
			return nil, fault.New("validation_error", "备份校验和不匹配。")
		}
	}
	if _, exists := files["journal.json"]; !exists {
		return nil, fault.New("validation_error", "备份缺少手账数据。")
	}
	return files, nil
}

func parseImport(ctx context.Context, format string, data []byte) (ImportDocument, map[string][]byte, error) {
	if len(data) > MaxImportBytes {
		return ImportDocument{}, nil, fault.New("import_too_large", "文件超过 160 MiB。")
	}
	files := map[string][]byte{}
	if format == "zip" {
		var err error
		files, err = readBundle(ctx, data)
		if err != nil {
			return ImportDocument{}, nil, err
		}
		data = files["journal.json"]
	}
	var exported Export
	if format == "csv" {
		if len(data) > MaxCSVBytes {
			return ImportDocument{}, nil, fault.New("import_too_large", "CSV 最多 2 MiB。")
		}
		var err error
		exported, err = parseCSV(data)
		if err != nil {
			return ImportDocument{}, nil, err
		}
	} else {
		if len(data) > MaxJournalBytes {
			return ImportDocument{}, nil, fault.New("import_too_large", "JSON 最多 64 MiB。")
		}
		if err := decodeTransfer(data, &exported); err != nil {
			return ImportDocument{}, nil, err
		}
		if exported.Schema != "animemo.journal/v1" {
			return ImportDocument{}, nil, fault.New("validation_error", "仅支持本项目 animemo.journal/v1 数据格式。")
		}
	}
	if len(exported.Entries) > 5000 || len(exported.History) > 20000 {
		return ImportDocument{}, nil, fault.New("validation_error", "最多导入 5000 部番剧和 20000 条观看记录。")
	}
	out := ImportDocument{Items: make([]ImportItem, 0, len(exported.Entries))}
	index := map[string]int{}
	for i, e := range exported.Entries {
		if err := ctx.Err(); err != nil {
			return out, nil, err
		}
		if !id.Valid(e.ID) {
			return out, nil, fault.New("validation_error", fmt.Sprintf("第 %d 条的标识无效。", i+1))
		}
		if _, exists := index[e.ID]; exists {
			return out, nil, fault.New("validation_error", "数据包中条目标识重复。")
		}
		if e.Format == "" {
			e.Format = "tv"
		}
		if e.Status == "" {
			e.Status = "planned"
		}
		if e.Accent == "" {
			e.Accent = "violet"
		}
		if err := e.Validate(); err != nil {
			return out, nil, err
		}
		if e.Source != nil && (e.Source.Provider != "bangumi" || e.Source.SubjectID < 1 || e.Source.SubjectID > 2147483647) {
			return out, nil, fault.Field("source", "备份中的 Bangumi 条目标识无效。")
		}
		item := ImportItem{Entry: e, History: []RecordInput{}}
		for _, ext := range []string{"png", "jpg"} {
			name := "covers/" + e.ID + "." + ext
			if content, exists := files[name]; exists {
				if item.CoverPath != "" {
					return out, nil, fault.New("validation_error", "同一条目存在多个封面。")
				}
				kind := "image/png"
				if ext == "jpg" {
					kind = "image/jpeg"
				}
				if _, err := media.Validate(content, kind); err != nil {
					return out, nil, err
				}
				item.CoverPath = name
			}
		}
		index[e.ID] = i
		out.Items = append(out.Items, item)
	}
	seenRecords := map[string]bool{}
	for _, r := range exported.History {
		i, exists := index[r.EntryID]
		if !exists || !id.Valid(r.ID) || seenRecords[r.ID] {
			return out, nil, fault.New("validation_error", "观看记录关联无效或重复。")
		}
		seenRecords[r.ID] = true
		input := RecordInput{WatchedOn: r.WatchedOn, EpisodeFrom: r.EpisodeFrom, EpisodeTo: r.EpisodeTo, Note: r.Note, Rewatch: r.Rewatch, RequestID: id.New()}
		if err := input.Validate(); err != nil {
			return out, nil, err
		}
		e := out.Items[i].Entry
		if e.TotalEpisodes > 0 && r.EpisodeTo > e.TotalEpisodes {
			return out, nil, fault.New("validation_error", "观看记录超出了番剧总话数。")
		}
		if r.EpisodeTo > e.WatchedEpisodes {
			return out, nil, fault.New("validation_error", "导入进度少于观看记录中的最远话数。")
		}
		out.Items[i].History = append(out.Items[i].History, input)
	}
	for name := range files {
		if strings.HasPrefix(name, "covers/") {
			found := false
			for _, item := range out.Items {
				if item.CoverPath == name {
					found = true
					break
				}
			}
			if !found {
				return out, nil, fault.New("validation_error", "备份包含未关联的封面。")
			}
		}
	}
	return out, files, nil
}

func parseCSV(data []byte) (Export, error) {
	if !utf8.Valid(data) || bytes.ContainsRune(data, 0) {
		return Export{}, fault.New("validation_error", "CSV 必须为 UTF-8 文本且不包含空字节。")
	}
	out := Export{Schema: "animemo.journal/v1", Entries: []Entry{}, History: []Record{}}
	reader := csv.NewReader(strings.NewReader(strings.TrimPrefix(string(data), "\ufeff")))
	headers, err := reader.Read()
	if err != nil {
		return out, fault.New("validation_error", "CSV 表头无效。")
	}
	allowed := map[string]bool{"title": true, "original_title": true, "format": true, "status": true, "total_episodes": true, "watched_episodes": true, "score": true, "notes": true, "tags": true, "accent": true, "studio": true, "airing_period": true, "description": true, "reference_url": true}
	seen := map[string]bool{}
	for _, h := range headers {
		if !allowed[h] || seen[h] {
			return out, fault.New("validation_error", "CSV 存在未知或重复的列。")
		}
		seen[h] = true
	}
	if !seen["title"] {
		return out, fault.New("validation_error", "CSV 需要 title 列。")
	}
	for row := 2; ; row++ {
		values, err := reader.Read()
		if err == io.EOF {
			break
		}
		if err != nil {
			return out, fault.New("validation_error", fmt.Sprintf("CSV 第 %d 行无效。", row))
		}
		if len(out.Entries) >= 500 {
			return out, fault.New("validation_error", "CSV 最多导入 500 部番剧。")
		}
		fields := map[string]string{}
		for i, h := range headers {
			fields[h] = values[i]
		}
		e := Entry{ID: id.New(), Title: fields["title"], OriginalTitle: fields["original_title"], Format: fields["format"], Status: fields["status"], Notes: fields["notes"], Accent: fields["accent"], Tags: strings.FieldsFunc(fields["tags"], func(r rune) bool { return r == '|' || r == ';' || r == ',' || r == '，' }), Details: Details{Studio: fields["studio"], AiringPeriod: fields["airing_period"], Description: fields["description"], ReferenceURL: fields["reference_url"]}}
		for name, target := range map[string]*int{"total_episodes": &e.TotalEpisodes, "watched_episodes": &e.WatchedEpisodes} {
			if value := strings.TrimSpace(fields[name]); value != "" {
				n, err := strconv.Atoi(value)
				if err != nil {
					return out, fault.Field(name, "CSV 话数必须是整数。")
				}
				*target = n
			}
		}
		if value := strings.TrimSpace(fields["score"]); value != "" {
			n, err := strconv.ParseFloat(value, 64)
			if err != nil {
				return out, fault.Field("score", "CSV 评分无效。")
			}
			e.Score = &n
		}
		out.Entries = append(out.Entries, e)
	}
	return out, nil
}

func importIdentity(e Entry) string {
	return strings.ToLower(strings.Join(strings.Fields(e.Title), " "))
}
