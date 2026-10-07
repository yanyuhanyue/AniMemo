// Package pluginproto is the versioned JSON boundary for stateless import plugins.
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
	Schema       int      `json:"schema"`
	Slug         string   `json:"slug"`
	Name         string   `json:"name"`
	Version      string   `json:"version"`
	Description  string   `json:"description"`
	Protocol     int      `json:"protocol"`
	HostMin      int      `json:"host_api_min"`
	HostMax      int      `json:"host_api_max"`
	Capabilities []string `json:"capabilities"`
	ModuleSHA256 string   `json:"module_sha256"`
}

// Package uses JSON/base64 instead of extracting an archive onto the host.
type Package struct {
	Manifest Manifest `json:"manifest"`
	Module   []byte   `json:"module"`
}
