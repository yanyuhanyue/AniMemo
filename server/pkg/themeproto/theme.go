// Package themeproto defines the private notes presentation contract; it has no converter dependency.
package themeproto

// NotesTheme describes the private notes reading surface. Optional inert HTML/CSS
// templates arrange core-owned slots and reference validated package resources.
type NotesTheme struct {
	Canvas       string        `json:"canvas"`
	Paper        string        `json:"paper"`
	Ink          string        `json:"ink"`
	Muted        string        `json:"muted"`
	Primary      string        `json:"primary"`
	Border       string        `json:"border"`
	Rule         string        `json:"rule"`
	HeadingFont  string        `json:"heading_font"`
	ReadingSize  string        `json:"reading_size"`
	Spacing      string        `json:"spacing"`
	Presentation *Presentation `json:"presentation,omitempty"`
}

// Presentation uses standard HTML slots and CSS, without executable code.
type Presentation struct {
	Schema int     `json:"schema"`
	Scope  string  `json:"scope"`
	List   string  `json:"list"`
	Card   string  `json:"card"`
	Reader string  `json:"reader"`
	CSS    string  `json:"css"`
	Assets []Asset `json:"assets,omitempty"`
}
type Asset struct {
	Name        string `json:"name"`
	ContentType string `json:"content_type"`
	SHA256      string `json:"sha256"`
}
