package term

import "time"

// Info is a terminal as `terminal.get` and `terminal.list` describe it.
type Info struct {
	ID               string            `json:"id"`
	Pid              int               `json:"pid"`
	Argv             []string          `json:"argv"`
	Cwd              string            `json:"cwd"`
	Title            string            `json:"title"`
	Status           string            `json:"status"` // running | exited
	ExitCode         *int              `json:"exit_code"`
	ExitSignal       *string           `json:"exit_signal"`
	CreatedAt        time.Time         `json:"created_at"`
	ExitedAt         *time.Time        `json:"exited_at"`
	LastOutputAt     *time.Time        `json:"last_output_at"`
	LastInputAt      *time.Time        `json:"last_input_at"`
	LastHumanInputAt *time.Time        `json:"last_human_input_at"`
	Cols             int               `json:"cols"`
	Rows             int               `json:"rows"`
	SizeOwner        *string           `json:"size_owner"`
	Clients          []ClientInfo      `json:"clients"`
	LastDetachAt     *time.Time        `json:"last_detach_at"`
	Keyboard         KeyboardState     `json:"keyboard"`
	Modes            ModesInfo         `json:"modes"`
	Busy             bool              `json:"busy"`
	LastCommand      *Command          `json:"last_command"`
	Labels           map[string]string `json:"labels"`
	LaunchID         string            `json:"launch_id"`
	Sandbox          bool              `json:"sandbox"`
	Shell            string            `json:"shell,omitempty"`
	ShellIntegration string            `json:"shell_integration,omitempty"`
	OutputSeq        int64             `json:"output_seq"` // offset one past the last output byte
	Preview          any               `json:"preview,omitempty"`
}

// ClientInfo is one attached client.
type ClientInfo struct {
	ID         string    `json:"id"`
	Kind       string    `json:"kind"`
	Label      string    `json:"label"`
	Via        string    `json:"via,omitempty"`
	ReadOnly   bool      `json:"read_only"`
	AttachedAt time.Time `json:"attached_at"`
}

func timePtr(t time.Time) *time.Time {
	if t.IsZero() {
		return nil
	}
	return &t
}

// Info describes the terminal now.
func (t *Terminal) Info() Info {
	running := t.Running()
	lastHuman := t.in.LastHuman()
	kb := t.in.Keyboard()
	foreground := false
	if running {
		// A job other than the terminal's own program is in the foreground: under a shell, a command
		// is running. In a sandbox the program is not the PTY's process but bubblewrap's grandchild,
		// with a group of its own; until it is found, bubblewrap's group is the only one known.
		if pg, err := t.proc.Foreground(); err == nil && pg > 0 && pg != t.Pid && pg != t.proc.ProgramGroup() {
			foreground = true
		}
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	// The shell's own marks say it best, once it prints them: a builtin loop runs in the shell's
	// process group, and a job put in the background with its output still coming is not the
	// prompt's business.
	busy := foreground
	if marked, ok := t.busyLocked(); ok {
		busy = marked && running
	}
	labels := t.Labels
	if labels == nil {
		labels = map[string]string{}
	}
	info := Info{
		ID: t.ID, Pid: t.Pid, Argv: t.Argv, Cwd: t.cwd, Title: t.title, Status: "running",
		CreatedAt: t.CreatedAt, LastOutputAt: timePtr(t.lastOutput), LastInputAt: timePtr(t.lastInput),
		LastHumanInputAt: timePtr(lastHuman.UTC()), Cols: t.cols, Rows: t.rows, Clients: []ClientInfo{},
		Keyboard: kb, Modes: t.modes, Busy: busy, LastCommand: t.lastCommand, Labels: labels,
		LaunchID: t.LaunchID, Sandbox: t.Sandbox, Shell: t.Shell, ShellIntegration: t.cmds.integration, OutputSeq: t.ring.Head(),
	}
	if lastHuman.IsZero() {
		info.LastHumanInputAt = nil
	}
	info.Clients = t.Clients()
	info.LastDetachAt = timePtr(t.LastDetach())
	if t.sizeOwner != "" {
		owner := t.sizeOwner
		info.SizeOwner = &owner
	}
	if !running {
		info.Status = "exited"
		code := t.exit.Code
		info.ExitCode = &code
		if t.exit.Signal != "" {
			sig := t.exit.Signal
			info.ExitSignal = &sig
		}
		info.ExitedAt = timePtr(t.exitedAt)
	}
	return info
}

// CwdFallback reports whether the requested working directory was missing and the home directory
// was used instead.
func (t *Terminal) CwdFallback() bool { return t.cwdFallback }
