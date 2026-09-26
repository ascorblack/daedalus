// Package record keeps keyframes of a group's pages for the operator to review afterwards: one after
// every action and one every few seconds while the page changes, on disk under the state directory,
// kept for a while and within a size, the oldest going first. Only the operator switches it on;
// the agent has no say in it, and it pauses while a person drives unless they chose otherwise.
package record

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Frame is one keyframe as the index lists it.
type Frame struct {
	No       int64  `json:"no"`
	At       int64  `json:"at"` // milliseconds since the epoch
	Tab      string `json:"tab"`
	URL      string `json:"url"`
	Kind     string `json:"kind"` // "action" (after an action), "change" (the page changed), "start"
	ActionID string `json:"action_id,omitempty"`
	W        int    `json:"w"`
	H        int    `json:"h"`
	Bytes    int    `json:"bytes"`
	// The page's CSS size when this picture was taken. A later resize of the window would otherwise
	// place an action's box on a picture of a different page. Absent on frames taken before that.
	VW int `json:"vw,omitempty"`
	VH int `json:"vh,omitempty"`
}

// GroupFrames summarises one group's recording on disk.
type GroupFrames struct {
	GroupID string `json:"group_id"`
	Frames  int    `json:"frames"`
	Bytes   int64  `json:"bytes"`
	FirstAt int64  `json:"first_at"`
	LastAt  int64  `json:"last_at"`
}

// Store is the recordings directory. Each group has a directory of its own holding numbered JPEG
// files and an index, one JSON line per frame; a frame whose file the retention removed is skipped
// when the index is read, so eviction never rewrites an index.
type Store struct {
	dir string

	mu    sync.Mutex
	next  map[string]int64
	total int64
	known bool
}

// Open returns the store under dir; nothing is read until it is used.
func Open(dir string) *Store { return &Store{dir: dir, next: map[string]int64{}} }

func (s *Store) groupDir(group string) string { return filepath.Join(s.dir, group) }

func frameFile(no int64) string { return fmt.Sprintf("%08d.jpg", no) }

// Put writes a frame of group, numbering it, and evicts the oldest frames of every group while the
// whole would be past maxBytes.
func (s *Store) Put(group string, f Frame, jpeg []byte, maxBytes int64) (Frame, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.known {
		s.total = s.sizeLocked()
		s.known = true
	}
	if maxBytes > 0 && s.total+int64(len(jpeg)) > maxBytes {
		s.evictLocked(time.Time{}, 0, maxBytes-int64(len(jpeg)))
	}
	dir := s.groupDir(group)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return Frame{}, err
	}
	no, ok := s.next[group]
	if !ok {
		no = lastNo(dir) + 1
	}
	f.No, f.Bytes = no, len(jpeg)
	if err := os.WriteFile(filepath.Join(dir, frameFile(no)), jpeg, 0o600); err != nil {
		return Frame{}, err
	}
	line, _ := json.Marshal(f)
	idx, err := os.OpenFile(filepath.Join(dir, "index.jsonl"), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return Frame{}, err
	}
	_, err = idx.Write(append(line, '\n'))
	if cerr := idx.Close(); err == nil {
		err = cerr
	}
	if err != nil {
		return Frame{}, err
	}
	s.next[group] = no + 1
	s.total += int64(len(jpeg))
	return f, nil
}

// lastNo is the highest frame number in a group's directory, so numbers never repeat across a
// daemon's restarts.
func lastNo(dir string) int64 {
	var last int64
	for _, f := range readIndex(dir) {
		last = max(last, f.No)
	}
	entries, _ := os.ReadDir(dir)
	for _, e := range entries {
		if n, err := strconv.ParseInt(strings.TrimSuffix(e.Name(), ".jpg"), 10, 64); err == nil {
			last = max(last, n)
		}
	}
	return last
}

func readIndex(dir string) []Frame {
	fh, err := os.Open(filepath.Join(dir, "index.jsonl"))
	if err != nil {
		return nil
	}
	defer fh.Close()
	var out []Frame
	sc := bufio.NewScanner(fh)
	sc.Buffer(make([]byte, 64<<10), 1<<20)
	for sc.Scan() {
		var f Frame
		if json.Unmarshal(sc.Bytes(), &f) == nil && f.No > 0 {
			out = append(out, f)
		}
	}
	return out
}

// List returns a group's frames after the number after, oldest first, at most limit of them.
func (s *Store) List(group string, after int64, limit int) []Frame {
	s.mu.Lock()
	defer s.mu.Unlock()
	dir := s.groupDir(group)
	out := []Frame{}
	for _, f := range readIndex(dir) {
		if f.No <= after {
			continue
		}
		if _, err := os.Stat(filepath.Join(dir, frameFile(f.No))); err != nil {
			continue
		}
		out = append(out, f)
		if limit > 0 && len(out) >= limit {
			break
		}
	}
	return out
}

// Read returns one frame and its JPEG; false when it is not kept.
func (s *Store) Read(group string, no int64) (Frame, []byte, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	dir := s.groupDir(group)
	data, err := os.ReadFile(filepath.Join(dir, frameFile(no)))
	if err != nil {
		return Frame{}, nil, false
	}
	for _, f := range readIndex(dir) {
		if f.No == no {
			return f, data, true
		}
	}
	return Frame{No: no, Bytes: len(data)}, data, true
}

// Delete removes a group's recording.
func (s *Store) Delete(group string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.next, group)
	err := os.RemoveAll(s.groupDir(group))
	s.known = false
	return err
}

// Groups lists every group with frames on disk.
func (s *Store) Groups() ([]GroupFrames, int64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	entries, _ := os.ReadDir(s.dir)
	out := []GroupFrames{}
	var total int64
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		g := GroupFrames{GroupID: e.Name()}
		dir := s.groupDir(e.Name())
		for _, f := range readIndex(dir) {
			st, err := os.Stat(filepath.Join(dir, frameFile(f.No)))
			if err != nil {
				continue
			}
			g.Frames++
			g.Bytes += st.Size()
			if g.FirstAt == 0 || f.At < g.FirstAt {
				g.FirstAt = f.At
			}
			g.LastAt = max(g.LastAt, f.At)
		}
		if g.Frames > 0 {
			out = append(out, g)
			total += g.Bytes
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].LastAt > out[j].LastAt })
	return out, total
}

// Sweep removes frames older than retention, then the oldest frames while the whole is past
// maxBytes, and the directories left without a frame. It answers how many frames it removed.
func (s *Store) Sweep(now time.Time, retention time.Duration, maxBytes int64) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	var cutoff time.Time
	if retention > 0 {
		cutoff = now.Add(-retention)
	}
	n := s.evictLocked(cutoff, 1, maxBytes)
	s.total = s.sizeLocked()
	s.known = true
	return n
}

type onDisk struct {
	path string
	mod  time.Time
	size int64
}

func (s *Store) frames() []onDisk {
	var out []onDisk
	groups, _ := os.ReadDir(s.dir)
	for _, g := range groups {
		if !g.IsDir() {
			continue
		}
		files, _ := os.ReadDir(filepath.Join(s.dir, g.Name()))
		for _, f := range files {
			if !strings.HasSuffix(f.Name(), ".jpg") {
				continue
			}
			if info, err := f.Info(); err == nil {
				out = append(out, onDisk{filepath.Join(s.dir, g.Name(), f.Name()), info.ModTime(), info.Size()})
			}
		}
	}
	return out
}

func (s *Store) sizeLocked() int64 {
	var total int64
	for _, f := range s.frames() {
		total += f.size
	}
	return total
}

// evictLocked removes the frames older than cutoff (when set), then the oldest until the whole is at
// most maxBytes (when positive), then, with prune, the group directories left empty.
func (s *Store) evictLocked(cutoff time.Time, prune int, maxBytes int64) int {
	all := s.frames()
	sort.Slice(all, func(i, j int) bool { return all[i].mod.Before(all[j].mod) })
	var total int64
	for _, f := range all {
		total += f.size
	}
	removed := 0
	for _, f := range all {
		old := !cutoff.IsZero() && f.mod.Before(cutoff)
		over := maxBytes > 0 && total > maxBytes
		if !old && !over {
			continue
		}
		if os.Remove(f.path) == nil {
			total -= f.size
			removed++
		}
	}
	s.total = total
	if prune > 0 {
		groups, _ := os.ReadDir(s.dir)
		for _, g := range groups {
			dir := filepath.Join(s.dir, g.Name())
			if files, _ := filepath.Glob(filepath.Join(dir, "*.jpg")); len(files) == 0 {
				_ = os.RemoveAll(dir)
				delete(s.next, g.Name())
			}
		}
	}
	return removed
}
