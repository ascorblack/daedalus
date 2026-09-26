package wire

import (
	"encoding/json"
	"fmt"
)

// The JSON-RPC 2.0 standard error codes, and the daemon's own. The numbers are the contract; the
// messages are for people.
const (
	CodeParseError     = -32700
	CodeInvalidRequest = -32600
	CodeMethodNotFound = -32601
	CodeInvalidParams  = -32602
	CodeInternal       = -32603

	CodeNotFound     = 1001 // no such terminal (or launch, or file)
	CodeExited       = 1002 // the terminal has exited
	CodeLimit        = 1003 // a limit was reached: terminals, in-flight requests, sizes of things
	CodeForbidden    = 1004 // not allowed in this state or by policy
	CodeTimeout      = 1005
	CodeKeyboardHeld = 1006 // a human holds the keyboard and the write timed out waiting for it
	CodeUnsupported  = 1007 // not available in this build or on this platform
	CodeStaleLaunch  = 1008
	CodeInvalidSize  = 1009 // below the minimum or above the maximum terminal size
)

// Request is a call or, without an id, a notification.
type Request struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params,omitempty"`
}

// IsNotification reports whether no reply is expected.
func (r *Request) IsNotification() bool { return len(r.ID) == 0 || string(r.ID) == "null" }

// Response is the reply to a call: exactly one of Result and Error is set.
type Response struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id"`
	Result  json.RawMessage `json:"result,omitempty"`
	Error   *Error          `json:"error,omitempty"`
}

// Notification is a message from the daemon that expects no reply: `hello` and `event`.
type Notification struct {
	JSONRPC string `json:"jsonrpc"`
	Method  string `json:"method"`
	Params  any    `json:"params"`
}

// Error is a JSON-RPC error object. It is also the Go error every handler returns when it wants a
// specific code on the wire; any other error becomes CodeInternal.
type Error struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
	Data    any    `json:"data,omitempty"`
}

func (e *Error) Error() string { return fmt.Sprintf("%s (%d)", e.Message, e.Code) }

// Errorf builds an Error with a formatted message.
func Errorf(code int, format string, args ...any) *Error {
	return &Error{Code: code, Message: fmt.Sprintf(format, args...)}
}

// EncodeNotification is the payload of a notification frame.
func EncodeNotification(method string, params any) ([]byte, error) {
	return json.Marshal(Notification{JSONRPC: "2.0", Method: method, Params: params})
}
