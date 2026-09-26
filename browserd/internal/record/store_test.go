package record

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func put(t *testing.T, s *Store, group string, size int, maxBytes int64) Frame {
	t.Helper()
	f, err := s.Put(group, Frame{At: time.Now().UnixMilli(), Kind: "action"}, make([]byte, size), maxBytes)
	if err != nil {
		t.Fatal(err)
	}
	return f
}

func age(t *testing.T, s *Store, group string, no int64, by time.Duration) {
	t.Helper()
	at := time.Now().Add(-by)
	if err := os.Chtimes(filepath.Join(s.groupDir(group), frameFile(no)), at, at); err != nil {
		t.Fatal(err)
	}
}

func TestFramesAreNumberedListedAndRead(t *testing.T) {
	s := Open(t.TempDir())
	for i := 0; i < 3; i++ {
		if f := put(t, s, "g1", 10, 0); f.No != int64(i+1) || f.Bytes != 10 {
			t.Fatalf("frame %d: %+v", i, f)
		}
	}
	if got := s.List("g1", 1, 0); len(got) != 2 || got[0].No != 2 {
		t.Fatalf("after 1: %+v", got)
	}
	if _, data, ok := s.Read("g1", 3); !ok || len(data) != 10 {
		t.Fatal("frame 3 not read")
	}
	// A restarted daemon never reuses a number.
	again := Open(s.dir)
	if f := put(t, again, "g1", 10, 0); f.No != 4 {
		t.Fatalf("after a restart: %+v", f)
	}
}

func TestRetentionAndTheCapEvictTheOldestFirst(t *testing.T) {
	s := Open(t.TempDir())
	for i := 0; i < 4; i++ {
		put(t, s, "old", 100, 0)
	}
	put(t, s, "new", 100, 0)
	// Two of the old group's frames are past a week; the sweep takes them and nothing else.
	age(t, s, "old", 1, 8*24*time.Hour)
	age(t, s, "old", 2, 8*24*time.Hour)
	age(t, s, "old", 3, time.Hour)
	age(t, s, "old", 4, 30*time.Minute)
	if n := s.Sweep(time.Now(), 7*24*time.Hour, 0); n != 2 {
		t.Fatalf("retention removed %d", n)
	}
	if got := s.List("old", 0, 0); len(got) != 2 || got[0].No != 3 {
		t.Fatalf("left %+v", got)
	}
	// Past the cap the oldest go first, whatever group they belong to: 300 bytes kept, one more
	// frame of 100 must evict the oldest one.
	put(t, s, "new", 100, 300)
	if got := s.List("old", 0, 0); len(got) != 1 || got[0].No != 4 {
		t.Fatalf("the cap left %+v", got)
	}
	groups, total := s.Groups()
	if total != 300 || len(groups) != 2 {
		t.Fatalf("groups %+v total %d", groups, total)
	}
	// A group whose every frame expired leaves no directory behind.
	age(t, s, "old", 4, 30*24*time.Hour)
	s.Sweep(time.Now(), 7*24*time.Hour, 0)
	if _, err := os.Stat(s.groupDir("old")); !os.IsNotExist(err) {
		t.Fatalf("the empty group stayed: %v", err)
	}
	if err := s.Delete("new"); err != nil {
		t.Fatal(err)
	}
	if groups, total := s.Groups(); len(groups) != 0 || total != 0 {
		t.Fatalf("after delete: %+v %d", groups, total)
	}
}
