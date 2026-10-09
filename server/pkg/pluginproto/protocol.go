// Package pluginproto is the versioned JSON boundary for file converters and declarative themes.
// It deliberately has no dependency on AniMemo internals, HTTP or a database.
package pluginproto

const Version = 1

type Request struct {
	Protocol int    `json:"protocol"`
	Filename string `json:"filename"`
	Text     string `json:"text"`
}

// Response carries an ordinary import document. Only the host validates and,
// after the user confirms a preview, commits it. A plugin cannot write records.
type Response struct {
	Protocol int    `json:"protocol"`
	Format   string `json:"format,omitempty"`
	Data     string `json:"data,omitempty"`
	Error    string `json:"error,omitempty"`
}

// Manifest is also the canonical package/compatibility vocabulary. Unknown
// fields and capabilities are rejected, never silently granted.
type Manifest struct {
	Schema       int         `json:"schema"`
	Slug         string      `json:"slug"`
	Name         string      `json:"name"`
	Version      string      `json:"version"`
	Description  string      `json:"description"`
	Protocol     int         `json:"protocol"`
	HostMin      int         `json:"host_api_min"`
	HostMax      int         `json:"host_api_max"`
	Capabilities []string    `json:"capabilities"`
	ModuleSHA256 string      `json:"module_sha256"`
	NotesTheme   *NotesTheme `json:"notes_theme,omitempty"`
}

// NotesTheme describes the private notes reading surface. It cannot contain CSS,
// scripts, resource URLs or selectors. Layout and all controls remain in Core.
type NotesTheme struct {
	Canvas      string `json:"canvas"`
	Paper       string `json:"paper"`
	Ink         string `json:"ink"`
	Muted       string `json:"muted"`
	Primary     string `json:"primary"`
	Border      string `json:"border"`
	Rule        string `json:"rule"`
	HeadingFont string `json:"heading_font"`
	ReadingSize string `json:"reading_size"`
	Spacing     string `json:"spacing"`
}

// Package uses JSON/base64 instead of extracting an archive onto the host.
type Package struct {
	Manifest Manifest `json:"manifest"`
	Module   []byte   `json:"module"`
}
