// Package converterproto is the standalone file-conversion wire protocol.
package converterproto

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
