package main

// What a start is doing, in the four or five pieces an operator can see.
//
// The launcher already keeps a running commentary — every line it prints is on the page — but a log
// answers "what is happening" and not "how much of this is left". A stage answers the second: the
// page draws the list once, ticks off what is behind the current one, and the commentary becomes
// the single live line under it. The stages are the real order the work happens in, and they differ
// between the two modes because the work does: a container installation fetches images where a
// native one downloads a runtime and builds an environment.

// Stage is one piece of a start. The empty stage means nothing is being started.
type Stage string

const (
	StageIdle        Stage = ""
	StageRuntime     Stage = "runtime"
	StageImages      Stage = "images"
	StageCheckouts   Stage = "checkouts"
	StageEnvironment Stage = "environment"
	StageStart       Stage = "start"
)

// nativeStages and dockerStages are the lists the progress page draws, in order. The page finds the
// current stage in the list and everything before it is done — which is why a stage is never set
// backwards, and why a step that turns out to be a no-op (a warm start downloads nothing) is still
// passed through rather than skipped.
var (
	nativeStages = []Stage{StageRuntime, StageCheckouts, StageEnvironment, StageStart}
	dockerStages = []Stage{StageCheckouts, StageImages, StageStart}
)

// Stages is the list for a mode, as the strings the page and the JSON both use.
func Stages(mode Mode) []string {
	list := dockerStages
	if mode == ModeNative {
		list = nativeStages
	}
	out := make([]string, 0, len(list))
	for _, stage := range list {
		out = append(out, string(stage))
	}
	return out
}

// stageKey is the message key for a stage's line on the page.
func stageKey(stage Stage) string { return "step." + string(stage) }
