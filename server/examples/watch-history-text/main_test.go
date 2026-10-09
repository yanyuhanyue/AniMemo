package main

import (
	"encoding/json"
	"testing"

	pluginproto "animemo.local/server/pkg/converterproto"
)

func TestTextRecordsAndAmbiguity(t *testing.T) {
	data, err := convert(pluginproto.Request{Filename: "2026记录.txt", Text: "10月1日\n首刷 《夏目友人帐》 第1-3集\n10月2日\n二刷 夏目友人帐 第1集 -- 重温\n二刷 夏目友人帐 第1集 -- 重复\n"})
	if err != nil {
		t.Fatal(err)
	}
	var out struct {
		Entries []entry  `json:"entries"`
		History []record `json:"history"`
	}
	json.Unmarshal([]byte(data), &out)
	if len(out.Entries) != 1 || out.Entries[0].Watched != 3 || len(out.History) != 3 || out.History[1].Rewatch != 2 || out.History[1].Note != "重温" || out.History[2].Note != "重复" || out.History[1].SourceLine != 4 || out.History[1].SourceFilename != "2026记录.txt" {
		t.Fatalf("bad import: %+v", out)
	}
	if _, err := convert(pluginproto.Request{Text: "2026-10-01\t测试\t1-12\t1\t笔记"}); err != nil {
		t.Fatal(err)
	}
	for _, text := range []string{"2月30日\n标题 第1集", "10月1日\n没有明确集数", "10月1日-3日\n标题 第1集", "2026-10-01\t标题\t4-1", "2026-10-01\t标题\t1\t0"} {
		if _, err := convert(pluginproto.Request{Filename: "2026.txt", Text: text}); err == nil {
			t.Fatalf("ambiguous input accepted: %q", text)
		}
	}
}
