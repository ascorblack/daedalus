package toolsmcp

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

// DefaultToolsHold is how long a call of a file's set waits for the host: above the host's own
// hold on a question to the operator (five minutes), so an action the operator is asked to approve
// is answered, not cut off, and below the listener's ceiling.
const DefaultToolsHold = 330 * time.Second

// toolsSource is the name a file set's posts carry at the hook listener.
const toolsSource = "tools"

// maxToolsFile bounds the launch file a set is read from.
const maxToolsFile = 1 << 20

var setName = regexp.MustCompile(`^[a-z][a-z0-9_-]{0,31}$`)

// toolsFile is `$DAEDALUS_LAUNCH_DIR/tools/<set>.json`, which the host writes into the launch: the
// tools the set offers with their descriptions and schemas. The host changes its tools without a new
// ptyd, because this server only reads them.
type toolsFile struct {
	Server       string `json:"server"`
	Instructions string `json:"instructions"`
	HoldMS       int64  `json:"hold_ms"`
	Unanswered   string `json:"unanswered"`
	Tools        []tool `json:"tools"`
}

// fileSet is a set described by the host in the launch's directory.
type fileSet struct {
	set   string
	file  toolsFile
	hold  time.Duration
	named map[string]tool
}

func loadFileSet(set string, env func(string) string, logf func(string, ...any)) (toolset, error) {
	empty := fileSet{set: set, named: map[string]tool{}, hold: DefaultToolsHold}
	if !setName.MatchString(set) {
		return empty, fmt.Errorf("%q is not a tool set's name", set)
	}
	dir := env("DAEDALUS_LAUNCH_DIR")
	if dir == "" {
		return empty, errors.New("DAEDALUS_LAUNCH_DIR is not set; this is not a launch, so there is no tool set to read")
	}
	path := filepath.Join(dir, "tools", set+".json")
	info, err := os.Stat(path)
	if err != nil {
		return empty, fmt.Errorf("no tool set %q in this launch (%s): %v", set, path, err)
	}
	if info.Size() > maxToolsFile {
		return empty, fmt.Errorf("the tool set %s is larger than %d bytes", path, maxToolsFile)
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return empty, err
	}
	var file toolsFile
	if err := json.Unmarshal(raw, &file); err != nil {
		return empty, fmt.Errorf("the tool set %s is not JSON: %v", path, err)
	}
	s := fileSet{set: set, file: file, named: map[string]tool{}, hold: DefaultToolsHold}
	if file.HoldMS > 0 {
		s.hold = time.Duration(file.HoldMS) * time.Millisecond
	}
	s.hold = holdFrom(env, "DAEDALUS_TOOLS_HOLD_MS", s.hold, logf)
	for _, t := range file.Tools {
		if t.Name == "" || t.InputSchema == nil {
			logf("a tool of %s without a name or a schema was left out", path)
			continue
		}
		s.named[t.Name] = t
	}
	return s, nil
}

func (s fileSet) name() string {
	if s.file.Server != "" {
		return s.file.Server
	}
	return "daedalus_" + strings.ReplaceAll(s.set, "-", "_")
}

func (s fileSet) instructions() string { return s.file.Instructions }

func (s fileSet) list() []tool {
	out := make([]tool, 0, len(s.file.Tools))
	for _, t := range s.file.Tools {
		if _, ok := s.named[t.Name]; ok {
			out = append(out, t)
		}
	}
	return out
}

func (fileSet) source() string { return toolsSource }

func (s fileSet) hello() map[string]any { return map[string]any{"set": s.set} }

func (s fileSet) unreachable() string { return "Daedalus is not reachable for the " + s.set + " tools" }

// decode checks a call against the tool's schema as far as a missing required argument and the
// arguments being an object, and posts the rest as it came: the host runs the tool, and checks it
// again, the way it checks its own sessions' calls.
func (s fileSet) decode(name string, raw json.RawMessage) (request, error) {
	t, ok := s.named[name]
	if !ok {
		return request{}, errUnknownTool
	}
	raw = bytes.TrimSpace(raw)
	if len(raw) == 0 || string(raw) == "null" {
		raw = []byte("{}")
	}
	var args map[string]any
	if err := json.Unmarshal(raw, &args); err != nil || args == nil {
		return request{}, errInvalid{"the arguments must be an object"}
	}
	if required, ok := t.InputSchema["required"].([]any); ok {
		var missing []string
		for _, r := range required {
			if key, ok := r.(string); ok {
				if _, present := args[key]; !present {
					missing = append(missing, key)
				}
			}
		}
		if len(missing) > 0 {
			return request{}, errInvalid{name + " needs " + strings.Join(missing, ", ")}
		}
	}
	fallback := s.file.Unanswered
	if fallback == "" {
		fallback = "Daedalus did not answer in time. The action may or may not have happened: look at the result before trying it again."
	}
	return request{
		op:          name,
		fields:      map[string]any{"set": s.set, "tool": name, "arguments": args},
		fallback:    fallback,
		fallbackErr: true,
		hold:        s.hold,
		source:      toolsSource,
	}, nil
}
