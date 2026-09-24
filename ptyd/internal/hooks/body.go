package hooks

import (
	"bytes"
	"encoding/json"
	"strings"
	"unicode/utf8"
)

// eventBody is a post's body as its event carries it: JSON as JSON when it parses, text otherwise;
// shortened to limit bytes when it is bigger. JSON is shortened by cutting its long strings, never
// by dropping keys, so the ids an adapter matches on (a tool use id, a session id, a hook's name)
// survive a tool response of megabytes. size is the encoded size of what is returned.
func eventBody(raw []byte, limit int) (body any, size int, truncated bool) {
	trimmed := bytes.TrimSpace(raw)
	if len(trimmed) > 0 && json.Valid(trimmed) {
		if len(trimmed) <= limit {
			return json.RawMessage(trimmed), len(trimmed), false
		}
		dec := json.NewDecoder(bytes.NewReader(trimmed))
		dec.UseNumber()
		var v any
		if err := dec.Decode(&v); err == nil {
			for cut := 16 << 10; cut >= 64; cut /= 4 {
				v = shorten(v, cut)
				if b, err := json.Marshal(v); err == nil && len(b) <= limit {
					return json.RawMessage(b), len(b), true
				}
			}
		}
		// Structure alone is bigger than the limit (a huge array of small things): nothing useful
		// fits, and the event says so rather than carrying half an object.
		return nil, 0, true
	}
	text := strings.ToValidUTF8(string(raw), "�")
	if len(text) <= limit {
		b, _ := json.Marshal(text)
		return text, len(b), false
	}
	text = cutUTF8(text, limit/2) // JSON escaping can double text; half always fits
	b, _ := json.Marshal(text)
	return text, len(b), true
}

// shorten cuts every string in v longer than cut bytes, marking where.
func shorten(v any, cut int) any {
	switch t := v.(type) {
	case string:
		if len(t) > cut {
			return cutUTF8(t, cut) + "…[cut]"
		}
		return t
	case []any:
		for i := range t {
			t[i] = shorten(t[i], cut)
		}
		return t
	case map[string]any:
		for k := range t {
			t[k] = shorten(t[k], cut)
		}
		return t
	}
	return v
}

// cutUTF8 is the longest prefix of s of at most n bytes that ends on a character boundary.
func cutUTF8(s string, n int) string {
	if len(s) <= n {
		return s
	}
	for n > 0 && !utf8.RuneStart(s[n]) {
		n--
	}
	return s[:n]
}
