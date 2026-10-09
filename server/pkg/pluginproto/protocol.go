// Package pluginproto is the versioned JSON boundary for file converters and declarative themes.
// It deliberately has no dependency on AniMemo internals, HTTP or a database.
package pluginproto

import (
	"animemo.local/server/pkg/converterproto"
	"animemo.local/server/pkg/themeproto"
)

const Version = converterproto.Version

type Request = converterproto.Request
type Response = converterproto.Response

// Manifest is also the canonical package/compatibility vocabulary. Unknown
// fields and capabilities are rejected, never silently granted.
type Manifest struct {
	Schema       int                      `json:"schema"`
	Slug         string                   `json:"slug"`
	Name         string                   `json:"name"`
	Version      string                   `json:"version"`
	Description  string                   `json:"description"`
	Protocol     int                      `json:"protocol"`
	HostMin      int                      `json:"host_api_min"`
	HostMax      int                      `json:"host_api_max"`
	Capabilities []string                 `json:"capabilities"`
	ModuleSHA256 string                   `json:"module_sha256"`
	NotesTheme   *NotesTheme              `json:"notes_theme,omitempty"`
	JournalTheme *themeproto.JournalTheme `json:"journal_theme,omitempty"`
}

type NotesTheme = themeproto.NotesTheme

// Package uses JSON/base64 instead of extracting an archive onto the host.
type Package struct {
	Manifest Manifest          `json:"manifest"`
	Module   []byte            `json:"module"`
	Assets   map[string][]byte `json:"assets,omitempty"`
}
