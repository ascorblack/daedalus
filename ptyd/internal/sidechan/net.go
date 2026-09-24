package sidechan

import (
	"errors"
	"fmt"
	"io/fs"
	"net"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"

	"github.com/ascorblack/daedalus/ptyd/internal/config"
	"github.com/ascorblack/daedalus/ptyd/internal/hooks"
)

// validSocket is what a socket in a launch's dial directory may be called: one name, never a path,
// so `unix:` can never reach outside that directory.
var validSocket = regexp.MustCompile(`^[A-Za-z0-9._-]{1,64}$`)

// Dialer opens net.dial streams for launches.
type Dialer struct {
	launches *hooks.Registry
	slots    chan struct{}
}

// NewDialer returns a dialer over the launch registry.
func NewDialer(launches *hooks.Registry) *Dialer {
	return &Dialer{launches: launches, slots: make(chan struct{}, config.MaxDials)}
}

// ErrStaleLaunch is a dial for a launch that is not registered (or has ended).
var ErrStaleLaunch = fmt.Errorf("%w: no such launch", ErrNotFound)

// Dial connects to target for the launch:
//
//   - `unix:<name>` is a socket in the launch's dial directory, which only its programs are told of;
//   - `tcp:127.0.0.1:<port>` is a loopback port the launch registered.
//
// Natively the loopback interface also holds the Daedalus API and the key proxy; only a registered
// port keeps an adapter from reaching them through the daemon. The returned release must be called
// once the stream has ended; the launch closes the connection itself when it ends first.
func (d *Dialer) Dial(target, launchID string) (net.Conn, *hooks.Launch, func(), error) {
	l, ok := d.launches.Get(launchID)
	if !ok {
		return nil, nil, nil, ErrStaleLaunch
	}
	var network, address string
	switch {
	case strings.HasPrefix(target, "unix:"):
		name := strings.TrimPrefix(target, "unix:")
		if !validSocket.MatchString(name) || name == "." || name == ".." {
			return nil, nil, nil, fmt.Errorf("%w: unix:<name> names a socket in the launch's dial directory", ErrInvalid)
		}
		address = filepath.Join(l.DialDir, name)
		if len(address) > 104 {
			return nil, nil, nil, fmt.Errorf("%w: %s is longer than a unix socket path may be", ErrInvalid, address)
		}
		st, err := os.Lstat(address)
		if err != nil {
			return nil, nil, nil, fmt.Errorf("%w: no socket %s", ErrNotFound, name)
		}
		if st.Mode()&fs.ModeSocket == 0 {
			return nil, nil, nil, fmt.Errorf("%w: %s is not a socket", ErrForbidden, name)
		}
		// The launch's programs can write where the socket lives. A dial directory they replaced by
		// a link would send the host's stream to whatever socket the link names instead.
		if real, err := filepath.EvalSymlinks(address); err != nil || real != d.launches.RealDialPath(l, name) {
			return nil, nil, nil, fmt.Errorf("%w: %s is not in the launch's dial directory", ErrForbidden, name)
		}
		network = "unix"
	case strings.HasPrefix(target, "tcp:127.0.0.1:"):
		port, err := strconv.Atoi(strings.TrimPrefix(target, "tcp:127.0.0.1:"))
		if err != nil || port < 1 || port > 65535 {
			return nil, nil, nil, fmt.Errorf("%w: tcp:127.0.0.1:<port>", ErrInvalid)
		}
		if !l.Allowed(port) {
			return nil, nil, nil, fmt.Errorf("%w: port %d is not registered for this launch", ErrForbidden, port)
		}
		network, address = "tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(port))
	default:
		return nil, nil, nil, fmt.Errorf("%w: a target is unix:<name> or tcp:127.0.0.1:<port>", ErrInvalid)
	}
	select {
	case d.slots <- struct{}{}:
	default:
		return nil, nil, nil, fmt.Errorf("%w: %d streams are open", ErrBusy, config.MaxDials)
	}
	nc, err := net.DialTimeout(network, address, config.DialTimeout)
	if err != nil {
		<-d.slots
		return nil, nil, nil, fmt.Errorf("dialling %s: %w", target, err)
	}
	unlaunch, err := l.AddStream(func() { nc.Close() })
	if err != nil {
		nc.Close()
		<-d.slots
		if errors.Is(err, hooks.ErrNoLaunch) {
			return nil, nil, nil, ErrStaleLaunch
		}
		return nil, nil, nil, fmt.Errorf("%w: %v", ErrBusy, err)
	}
	var once sync.Once
	release := func() {
		once.Do(func() {
			unlaunch()
			<-d.slots
		})
	}
	return nc, l, release, nil
}
