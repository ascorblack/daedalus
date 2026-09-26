package netwall

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"time"
)

// ChromiumArgs are the switches that put a browser behind its proxy, added to the daemon's own
// (docs/architecture/browser.md, Which Chromium). Each closes a way around the wall:
//
//   - --proxy-server sends every http, https and WebSocket connection to the proxy, and with it the
//     name rather than an address: Chromium looks nothing up for a proxied request.
//   - --proxy-bypass-list=<-loopback> takes away Chromium's built-in exception for loopback, which
//     would otherwise let a page reach this machine's ports directly.
//   - --host-resolver-rules maps every name to "not found" for anything that would still resolve
//     one itself. Nothing proxied does; this is the floor under what is not (a STUN server's name,
//     a feature added in a later Chromium). The rule applies to address literals as well, the
//     proxy's own included (measured: with a bare "MAP * ~NOTFOUND" no page loads), so the proxy's
//     address is the one exception.
//   - --disable-quic, because QUIC is UDP and cannot go through an HTTP proxy. Chromium does not use
//     it through a proxy today; the switch keeps it that way.
//   - --webrtc-ip-handling-policy=disable_non_proxied_udp keeps WebRTC's UDP off the network. It is
//     in the daemon's base switches too; it is here as well because it is part of this wall, and
//     a later edit of the base list must not quietly remove it. (--force-webrtc-ip-handling-policy
//     is not honoured by Chromium 151: STUN packets reached the wire with it.)
func ChromiumArgs(proxyAddr string) []string {
	host, _, err := net.SplitHostPort(proxyAddr)
	if err != nil {
		host = proxyAddr
	}
	return []string{
		"--proxy-server=http://" + proxyAddr,
		"--proxy-bypass-list=<-loopback>",
		"--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE " + host,
		"--disable-quic",
		"--webrtc-ip-handling-policy=disable_non_proxied_udp",
	}
}

// Grant is net.grant's parameters: the operator allowed an asked destination for the browser that
// holds group_id, for ttl_ms (default an hour, at most a day). The host sends it after the
// operator's answer; the agent then retries.
type Grant struct {
	GroupID string `json:"group_id"`
	Host    string `json:"host"`
	Port    int    `json:"port"`
	TTLms   int64  `json:"ttl_ms,omitempty"`
}

// DecodeGrant reads net.grant's parameters strictly.
func DecodeGrant(raw json.RawMessage) (Grant, error) {
	var g Grant
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&g); err != nil {
		return Grant{}, err
	}
	if dec.More() {
		return Grant{}, errors.New("trailing data after the parameters")
	}
	if g.GroupID == "" || normalHost(g.Host) == "" || g.Port < 1 || g.Port > 65535 {
		return Grant{}, fmt.Errorf("net.grant needs group_id, host and port")
	}
	if g.TTLms < 0 || time.Duration(g.TTLms)*time.Millisecond > grantMax {
		return Grant{}, fmt.Errorf("ttl_ms: at most %d", grantMax.Milliseconds())
	}
	return g, nil
}

// TTL is how long the grant lasts.
func (g Grant) TTL() time.Duration {
	if g.TTLms == 0 {
		return time.Hour
	}
	return time.Duration(g.TTLms) * time.Millisecond
}
