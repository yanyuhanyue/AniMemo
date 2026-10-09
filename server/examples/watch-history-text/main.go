// A reference plugin built as GOOS=wasip1 GOARCH=wasm. No host internals.
package main

import (
	"bufio"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"

	pluginproto "animemo.local/server/pkg/converterproto"
)

type entry struct {
	ID      string `json:"id"`
	Title   string `json:"title"`
	Status  string `json:"status"`
	Watched int    `json:"watched_episodes"`
}
type record struct {
	SourceLine     int    `json:"source_line"`
	SourceFilename string `json:"source_filename"`
	ID             string `json:"id"`
	EntryID        string `json:"entry_id"`
	Date           string `json:"watched_on"`
	From           int    `json:"episode_from"`
	To             int    `json:"episode_to"`
	Rewatch        int    `json:"rewatch"`
	Note           string `json:"note"`
}

var yearRE = regexp.MustCompile(`(?:19|20)[0-9]{2}`)
var dateRE = regexp.MustCompile(`^(\d{1,2})月(\d{1,2})(?:日|号)?\s*(.*)$`)
var episodesRE = regexp.MustCompile(`^(.*?)(?:第\s*(\d+)(?:\s*[-~～至]\s*(\d+))?\s*[集话]|共\s*(\d+)\s*[集话])\s*$`)
var brushRE = regexp.MustCompile(`^(首|二|三|四|五|六|七|八|九|十|\d+)刷\s*(.*)$`)
var rangeRE = regexp.MustCompile(`^(\d+)(?:-(\d+))?$`)

func stableID(value string) string {
	b := sha256.Sum256([]byte(value))
	b[6] = (b[6] & 15) | 0x50
	b[8] = (b[8] & 63) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[:4], b[4:6], b[6:8], b[8:10], b[10:16])
}

func convert(in pluginproto.Request) (string, error) {
	entries := []entry{}
	records := []record{}
	index := map[string]int{}
	seen := map[string]bool{}
	year := yearRE.FindString(in.Filename)
	currentDate := ""
	scanner := bufio.NewScanner(strings.NewReader(strings.TrimPrefix(in.Text, "\ufeff")))
	scanner.Buffer(make([]byte, 4096), 32<<10)
	for line := 1; scanner.Scan(); line++ {
		text := strings.TrimSpace(scanner.Text())
		if text == "" || strings.HasPrefix(text, "#") || strings.HasPrefix(text, "忆往昔") {
			continue
		}
		date, title, note := "", "", ""
		from, to, rewatch := 0, 0, 1
		if strings.Contains(text, "\t") {
			parts := strings.Split(text, "\t")
			if len(parts) < 3 || len(parts) > 5 {
				return "", fmt.Errorf("第 %d 行需要 日期、标题、话数，使用制表符分隔；可追加刷次和笔记。", line)
			}
			date, title = strings.TrimSpace(parts[0]), strings.TrimSpace(parts[1])
			m := rangeRE.FindStringSubmatch(strings.TrimSpace(parts[2]))
			if m != nil {
				from, _ = strconv.Atoi(m[1])
				to = from
				if m[2] != "" {
					to, _ = strconv.Atoi(m[2])
				}
			}
			if len(parts) > 3 {
				rewatch, _ = strconv.Atoi(strings.TrimSpace(parts[3]))
			}
			if len(parts) > 4 {
				note = parts[4]
			}
		} else {
			if m := dateRE.FindStringSubmatch(text); m != nil {
				if year == "" {
					return "", fmt.Errorf("第 %d 行只有月日，请在文件名中包含四位年份。", line)
				}
				month, _ := strconv.Atoi(m[1])
				day, _ := strconv.Atoi(m[2])
				currentDate = fmt.Sprintf("%s-%02d-%02d", year, month, day)
				text = strings.TrimSpace(m[3])
				if text == "" {
					continue
				}
			}
			date = currentDate
			if m := brushRE.FindStringSubmatch(text); m != nil {
				words := map[string]int{"首": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
				rewatch = words[m[1]]
				if rewatch == 0 {
					rewatch, _ = strconv.Atoi(m[1])
				}
				text = m[2]
			}
			if parts := strings.SplitN(text, "--", 2); len(parts) == 2 {
				text = strings.TrimSpace(parts[0])
				note = strings.TrimSpace(parts[1])
			}
			if m := episodesRE.FindStringSubmatch(text); m != nil {
				title = strings.Trim(strings.TrimSpace(m[1]), "《》")
				if m[4] != "" {
					from = 1
					to, _ = strconv.Atoi(m[4])
				} else {
					from, _ = strconv.Atoi(m[2])
					to = from
					if m[3] != "" {
						to, _ = strconv.Atoi(m[3])
					}
				}
			}
		}
		parsed, err := time.Parse("2006-01-02", date)
		if err != nil || parsed.Year() < 1900 || parsed.Year() > 2100 || title == "" || from < 1 || to < from || to > 10000 || rewatch < 1 || rewatch > 100 || len([]rune(title)) > 200 || len([]rune(note)) > 2000 {
			return "", fmt.Errorf("第 %d 行的日期、标题、话数或刷次不明确，请按格式说明修改后重试。", line)
		}
		key := strings.ToLower(strings.Join(strings.Fields(title), " "))
		at, ok := index[key]
		if !ok {
			at = len(entries)
			index[key] = at
			entries = append(entries, entry{ID: stableID("title:" + key), Title: title, Status: "watching"})
		}
		if to > entries[at].Watched {
			entries[at].Watched = to
		}
		rid := stableID(fmt.Sprintf("%s:%s:%d:%d:%d", key, date, from, to, rewatch) + ":" + note)
		if !seen[rid] {
			records = append(records, record{SourceLine: line, SourceFilename: in.Filename, ID: rid, EntryID: entries[at].ID, Date: date, From: from, To: to, Rewatch: rewatch, Note: note})
			seen[rid] = true
		}
		if len(entries) > 500 || len(records) > 2000 {
			return "", fmt.Errorf("单次最多解析 500 部番剧、2000 条观看记录。")
		}
	}
	if scanner.Err() != nil {
		return "", fmt.Errorf("文本行过长或读取失败。")
	}
	if len(entries) == 0 {
		return "", fmt.Errorf("没有可导入的观看记录。")
	}
	data, err := json.Marshal(struct {
		Schema  string   `json:"schema"`
		Entries []entry  `json:"entries"`
		History []record `json:"history"`
	}{"animemo.journal/v1", entries, records})
	return string(data), err
}

func main() {
	var in pluginproto.Request
	out := pluginproto.Response{Protocol: pluginproto.Version}
	if err := json.NewDecoder(os.Stdin).Decode(&in); err != nil || in.Protocol != pluginproto.Version {
		out.Error = "输入协议无效。"
	} else if data, err := convert(in); err != nil {
		out.Error = err.Error()
	} else {
		out.Format = "json"
		out.Data = data
	}
	_ = json.NewEncoder(os.Stdout).Encode(out)
}
