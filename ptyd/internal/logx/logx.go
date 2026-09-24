// Package logx is the daemon's log and its journal of agent writes.
//
// The log never carries terminal content or tokens: it is read by people diagnosing the daemon, and
// terminals are where passwords get typed. What an agent wrote into a terminal is different — it is
// the record the architecture asks for — so it goes to its own journal, never to the log.
package logx

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strconv"
	"sync"
	"time"
)

// New returns a JSON-lines logger writing to path, or to stderr when path is empty.
func New(path, level string) (*slog.Logger, io.Closer, error) {
	var lv slog.Level
	if err := lv.UnmarshalText([]byte(level)); err != nil {
		return nil, nil, fmt.Errorf("log level %q: %w", level, err)
	}
	var w io.Writer = os.Stderr
	var c io.Closer = io.NopCloser(nil)
	if path != "" {
		f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
		if err != nil {
			return nil, nil, err
		}
		w, c = f, f
	}
	return slog.New(slog.NewJSONHandler(w, &slog.HandlerOptions{Level: lv})), c, nil
}

// Rotating is an append-only file that rolls over at a size: name, name.1 … name.(files-1).
type Rotating struct {
	mu    sync.Mutex
	path  string
	limit int64
	files int
	f     *os.File
	size  int64
}

// OpenRotating opens (or creates) path for appending.
func OpenRotating(path string, limit int64, files int) (*Rotating, error) {
	r := &Rotating{path: path, limit: limit, files: max(1, files)}
	if err := r.open(); err != nil {
		return nil, err
	}
	return r, nil
}

func (r *Rotating) open() error {
	if err := os.MkdirAll(filepath.Dir(r.path), 0o700); err != nil {
		return err
	}
	f, err := os.OpenFile(r.path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return err
	}
	st, err := f.Stat()
	if err != nil {
		f.Close()
		return err
	}
	r.f, r.size = f, st.Size()
	return nil
}

// Write appends p, rotating first when p would take the file past its limit.
func (r *Rotating) Write(p []byte) (int, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.f == nil {
		return 0, os.ErrClosed
	}
	if r.size > 0 && r.size+int64(len(p)) > r.limit {
		if err := r.rotate(); err != nil {
			return 0, err
		}
	}
	n, err := r.f.Write(p)
	r.size += int64(n)
	return n, err
}

func (r *Rotating) rotate() error {
	r.f.Close()
	r.f = nil
	// name.1 becomes name.2 and so on, the oldest falling off the end; with a single file the
	// current one is simply started over.
	for i := r.files - 1; i >= 1; i-- {
		from := r.path
		if i > 1 {
			from = r.path + "." + strconv.Itoa(i-1)
		}
		_ = os.Rename(from, r.path+"."+strconv.Itoa(i))
	}
	if r.files == 1 {
		_ = os.Remove(r.path)
	}
	return r.open()
}

// Close closes the file.
func (r *Rotating) Close() error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.f == nil {
		return nil
	}
	err := r.f.Close()
	r.f = nil
	return err
}

// JournalTextBytes is how much of an agent write the journal keeps verbatim; the digest covers all
// of it.
const JournalTextBytes = 4 << 10

// Journal records every agent write.
type Journal struct {
	w io.Writer
}

// NewJournal returns a journal writing JSON lines to w.
func NewJournal(w io.Writer) *Journal { return &Journal{w: w} }

// AgentWrite is one journal line.
type AgentWrite struct {
	At        time.Time `json:"at"`
	Terminal  string    `json:"terminal"`
	Actor     string    `json:"actor"`
	LaunchID  string    `json:"launch_id,omitempty"`
	Note      string    `json:"note,omitempty"`
	Kind      string    `json:"kind"` // text, paste, keys, bytes
	Bytes     int       `json:"bytes"`
	Text      string    `json:"text"`
	Truncated bool      `json:"truncated,omitempty"`
	SHA256    string    `json:"sha256"`
}

// Record writes one line for data written by an agent. Errors are returned for the caller to log;
// a journal that cannot be written must not stop the write it describes.
func (j *Journal) Record(e AgentWrite, data []byte) error {
	if j == nil || j.w == nil {
		return nil
	}
	sum := sha256.Sum256(data)
	e.SHA256 = hex.EncodeToString(sum[:])
	e.Bytes = len(data)
	if len(data) > JournalTextBytes {
		data, e.Truncated = data[:JournalTextBytes], true
	}
	e.Text = string(data)
	line, err := json.Marshal(e)
	if err != nil {
		return err
	}
	_, err = j.w.Write(append(line, '\n'))
	return err
}
