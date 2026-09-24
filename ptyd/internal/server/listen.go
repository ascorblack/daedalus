// Package server is the daemon's socket: the run directory and its files, the token handshake, the
// channel framing, and the JSON-RPC dispatcher.
package server

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// The files of a run directory. The host is told only the directory; everything else it learns from
// these.
const (
	EndpointFile = "endpoint"  // "unix:ptyd.sock" or "tcp:127.0.0.1:<port>", written last
	TokenFile    = "token"     // 64 hex characters, 0600, new at every start
	SocketFile   = "ptyd.sock" // 0600
	LockFile     = "ptyd.lock"
)

// Endpoint is a listening socket with its run directory prepared.
type Endpoint struct {
	Listener net.Listener
	Token    string
	RunDir   string
	release  func()
}

// ErrHeld is returned when another daemon is serving the run directory.
var ErrHeld = errors.New("another ptyd holds the run directory")

// Prepare claims runDir and listens. The order matters to a host that is watching the directory:
// the token is written before the endpoint, and the endpoint only once the socket accepts, so an
// endpoint file that exists always leads to a daemon that is ready and a token that matches it.
func Prepare(runDir, listen string) (*Endpoint, error) {
	if err := os.MkdirAll(runDir, 0o700); err != nil {
		return nil, err
	}
	// The directory holds the token; nobody else needs to list it.
	if err := os.Chmod(runDir, 0o700); err != nil {
		return nil, err
	}
	unlock, err := lockDir(filepath.Join(runDir, LockFile))
	if err != nil {
		return nil, fmt.Errorf("%w %s: %v", ErrHeld, runDir, err)
	}
	ok := false
	defer func() {
		if !ok {
			unlock()
		}
	}()
	// Whatever a previous daemon left is either live (refuse) or stale (remove).
	var ln net.Listener
	var endpoint string
	if listen == "unix" {
		sock := filepath.Join(runDir, SocketFile)
		if c, err := net.DialTimeout("unix", sock, time.Second); err == nil {
			c.Close()
			return nil, fmt.Errorf("%w %s", ErrHeld, runDir)
		}
		_ = os.Remove(filepath.Join(runDir, EndpointFile))
		if err := os.Remove(sock); err != nil && !errors.Is(err, os.ErrNotExist) {
			return nil, err
		}
		if len(sock) > 104 {
			// sun_path is 108 bytes on Linux and 104 on macOS; a longer path fails with a confusing
			// "invalid argument", so say what is wrong.
			return nil, fmt.Errorf("socket path %s is %d bytes, longer than a unix socket allows", sock, len(sock))
		}
		ln, err = net.Listen("unix", sock)
		if err != nil {
			return nil, err
		}
		if err := os.Chmod(sock, 0o600); err != nil {
			ln.Close()
			return nil, err
		}
		endpoint = "unix:" + SocketFile
	} else {
		_ = os.Remove(filepath.Join(runDir, EndpointFile))
		addr := strings.TrimPrefix(listen, "tcp:")
		ln, err = net.Listen("tcp", addr)
		if err != nil {
			return nil, err
		}
		endpoint = "tcp:" + ln.Addr().String()
	}
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		ln.Close()
		return nil, err
	}
	token := hex.EncodeToString(raw)
	if err := writeAtomic(filepath.Join(runDir, TokenFile), []byte(token+"\n")); err != nil {
		ln.Close()
		return nil, err
	}
	if err := writeAtomic(filepath.Join(runDir, EndpointFile), []byte(endpoint+"\n")); err != nil {
		ln.Close()
		return nil, err
	}
	ok = true
	e := &Endpoint{Listener: ln, Token: token, RunDir: runDir}
	e.release = func() {
		// The endpoint goes first, so that a watching host never finds an endpoint without a daemon.
		_ = os.Remove(filepath.Join(runDir, EndpointFile))
		_ = os.Remove(filepath.Join(runDir, TokenFile))
		if listen == "unix" {
			_ = os.Remove(filepath.Join(runDir, SocketFile))
		}
		unlock()
	}
	return e, nil
}

// Release removes the endpoint, the token and the socket, and gives up the directory.
func (e *Endpoint) Release() {
	if e.release != nil {
		e.release()
		e.release = nil
	}
}

// writeAtomic writes a 0600 file through a temporary name, so a reader never sees half of it.
func writeAtomic(path string, data []byte) error {
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// ReadEndpoint reads a run directory the way a client does: the endpoint and the token.
func ReadEndpoint(runDir string) (network, address, token string, err error) {
	ep, err := os.ReadFile(filepath.Join(runDir, EndpointFile))
	if err != nil {
		return "", "", "", err
	}
	tok, err := os.ReadFile(filepath.Join(runDir, TokenFile))
	if err != nil {
		return "", "", "", err
	}
	e := strings.TrimSpace(string(ep))
	switch {
	case strings.HasPrefix(e, "unix:"):
		// Relative to the run directory, so a directory bind-mounted elsewhere still works.
		name := strings.TrimPrefix(e, "unix:")
		if !filepath.IsAbs(name) {
			name = filepath.Join(runDir, name)
		}
		return "unix", name, strings.TrimSpace(string(tok)), nil
	case strings.HasPrefix(e, "tcp:"):
		return "tcp", strings.TrimPrefix(e, "tcp:"), strings.TrimSpace(string(tok)), nil
	}
	return "", "", "", fmt.Errorf("endpoint %q is neither unix: nor tcp:", e)
}
