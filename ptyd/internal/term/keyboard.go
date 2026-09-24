package term

import (
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/config"
)

// keyboardState is the keyboard state as clients receive it: `until` in milliseconds since the
// epoch, or null for a state without expiry.
func keyboardState(k KeyboardState) map[string]any {
	var until any
	if !k.Until.IsZero() {
		until = k.Until.UnixMilli()
	}
	return map[string]any{"owner": k.Owner, "until": until}
}

func keyboardEvent(k KeyboardState) map[string]any {
	e := keyboardState(k)
	e["type"] = "keyboard"
	return e
}

// keyboardChanged runs when the keyboard changes hands (a grant, a human ending an agent grant). The
// clients are told, and told again when a grant with an expiry runs out, since nothing else would
// tell them that the agents may type again.
func (t *Terminal) keyboardChanged(k KeyboardState) {
	t.broadcastKeyboard()
	a := &t.att
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.kbTimer != nil {
		a.kbTimer.Stop()
		a.kbTimer = nil
	}
	if a.closed || k.Until.IsZero() {
		return
	}
	// A little after the instant, so the state read when the event is built has already expired.
	d := k.Until.Sub(t.deps.Clock.Now()) + 10*time.Millisecond
	a.kbTimer = time.AfterFunc(max(d, 0), t.broadcastKeyboard)
}

func (t *Terminal) broadcastKeyboard() {
	t.broadcast("keyboard", func(*Client) any { return func() any { return keyboardEvent(t.in.Keyboard()) } })
}

// agentTyped runs after every delivered agent write: clients show that an agent is typing, and stop
// showing it once the agent has been quiet for a moment.
func (t *Terminal) agentTyped(actor string) {
	a := &t.att
	a.mu.Lock()
	if a.closed {
		a.mu.Unlock()
		return
	}
	announce := a.typingActor != actor
	a.typingActor = actor
	if a.typingTimer != nil {
		a.typingTimer.Stop()
	}
	a.typingTimer = time.AfterFunc(config.AgentTypingQuiet, func() {
		a.mu.Lock()
		if a.typingActor != actor {
			a.mu.Unlock()
			return
		}
		a.typingActor = ""
		a.mu.Unlock()
		t.broadcast("agent_typing", func(*Client) any {
			return map[string]any{"type": "agent_typing", "actor": actor, "active": false}
		})
	})
	a.mu.Unlock()
	if announce {
		t.broadcast("agent_typing", func(*Client) any {
			return map[string]any{"type": "agent_typing", "actor": actor, "active": true}
		})
	}
}
