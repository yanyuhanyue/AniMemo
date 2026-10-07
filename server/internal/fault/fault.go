// Package fault contains errors that callers can safely show to the user.
package fault

type Error struct {
	Code    string            `json:"code"`
	Message string            `json:"message"`
	Fields  map[string]string `json:"fields,omitempty"`
}

func (e *Error) Error() string { return e.Code + ": " + e.Message }

func New(code, message string) *Error { return &Error{Code: code, Message: message} }

func Field(field, message string) *Error {
	return &Error{Code: "validation_error", Message: message, Fields: map[string]string{field: message}}
}
