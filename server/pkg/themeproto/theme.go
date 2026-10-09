// Package themeproto defines the private presentation contracts; it has no converter dependency.
package themeproto

// Tokens are shared across supported private reading surfaces.
type Tokens struct {
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

type NotesTheme struct {
	Tokens
	Presentation *Presentation `json:"presentation,omitempty"`
}

// JournalTheme covers the private entry list and its detail overview.
// Business operations, sharing and destructive confirmations remain in Core.
type JournalTheme struct {
	Tokens
	Presentation JournalPresentation `json:"presentation"`
}

type JournalPresentation struct {
	Schema int     `json:"schema"`
	Scope  string  `json:"scope"`
	List   string  `json:"list"`
	Card   string  `json:"card"`
	Row    string  `json:"row"`
	Detail string  `json:"detail"`
	CSS    string  `json:"css"`
	Assets []Asset `json:"assets,omitempty"`
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
