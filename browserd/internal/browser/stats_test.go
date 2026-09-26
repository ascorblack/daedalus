package browser

import (
	"reflect"
	"testing"
)

func TestFitToCgroup(t *testing.T) {
	// Two browsers whose private figures over-count what the kernel charged (the renderers share the
	// zygote's pages): 600 and 300 estimated, 520 charged, of which 20 are the daemon's own.
	got, ok := fitToCgroup([]int64{600, 300}, 20, 520)
	if !ok || !reflect.DeepEqual(got, []int64{333, 166}) {
		t.Fatalf("fitted %v %v", got, ok)
	}
	// The kernel charging more than estimated (shared memory files in /dev/shm) is the truth too.
	if got, ok := fitToCgroup([]int64{100}, 10, 210); !ok || got[0] != 200 {
		t.Fatalf("fitted %v %v", got, ok)
	}
	// Figures that contradict each other leave the estimates alone.
	for _, c := range []struct {
		est            []int64
		others, charge int64
	}{{[]int64{100}, 300, 200}, {[]int64{0}, 0, 100}, {nil, 0, 100}} {
		if got, ok := fitToCgroup(c.est, c.others, c.charge); ok || !reflect.DeepEqual(got, c.est) {
			t.Fatalf("%+v: fitted %v %v", c, got, ok)
		}
	}
}
