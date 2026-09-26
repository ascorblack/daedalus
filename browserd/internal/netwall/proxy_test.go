package netwall

import (
	"bufio"
	"crypto/tls"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// fixture is a loopback server standing in for a host on the internet. The fake resolver gives the
// host a public address; the wall judges that address, and the test redirect dials the fixture.
type fixture struct {
	hits atomic.Int64
	srv  *httptest.Server
}

func newFixture(t *testing.T, tlsOn bool, body string) *fixture {
	f := &fixture{}
	h := http.HandlerFunc(func(rw http.ResponseWriter, r *http.Request) {
		f.hits.Add(1)
		fmt.Fprintf(rw, "%s host=%s path=%s xff=%q", body, r.Host, r.URL.Path, r.Header.Get("X-Forwarded-For"))
	})
	if tlsOn {
		f.srv = httptest.NewTLSServer(h)
	} else {
		f.srv = httptest.NewServer(h)
	}
	t.Cleanup(f.srv.Close)
	return f
}

func (f *fixture) addr() netip.AddrPort {
	return netip.MustParseAddrPort(f.srv.Listener.Addr().String())
}

// proxied is an http.Client that goes through p, as Chromium does.
func proxied(p *Proxy) *http.Client {
	u, _ := url.Parse("http://" + p.Addr())
	return &http.Client{
		Transport: &http.Transport{Proxy: http.ProxyURL(u), TLSClientConfig: &tls.Config{InsecureSkipVerify: true}, DisableKeepAlives: true},
		Timeout:   10 * time.Second,
	}
}

type setup struct {
	w       *Wall
	p       *Proxy
	res     *fakeResolver
	events  *[]Egress
	public  *fixture
	secure  *fixture
	private *fixture
	echo    net.Listener
}

func newSetup(t *testing.T, c Config) *setup {
	t.Helper()
	s := &setup{public: newFixture(t, false, "public"), secure: newFixture(t, true, "secure"), private: newFixture(t, false, "private")}
	echo, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { echo.Close() })
	go func() {
		for {
			c, err := echo.Accept()
			if err != nil {
				return
			}
			go func() { io.Copy(c, c); c.Close() }()
		}
	}()
	s.echo = echo
	s.w, s.res, s.events = newTestWall(t, c, map[string][]string{
		"public.test":  {"203.0.113.10"},
		"secure.test":  {"203.0.113.11"},
		"echo.test":    {"203.0.113.12"},
		"private.test": {"10.0.0.1"},
		"rebind.test":  {"203.0.113.10"},
	})
	routes := map[netip.AddrPort]netip.AddrPort{
		netip.MustParseAddrPort("203.0.113.10:80"):  s.public.addr(),
		netip.MustParseAddrPort("203.0.113.11:443"): s.secure.addr(),
		netip.MustParseAddrPort("203.0.113.12:7"):   netip.MustParseAddrPort(echo.Addr().String()),
		netip.MustParseAddrPort("10.0.0.1:80"):      s.private.addr(),
	}
	s.w.redirect = func(a netip.AddrPort) netip.AddrPort {
		if to, ok := routes[a]; ok {
			return to
		}
		return a
	}
	p, err := s.w.Listen("b7")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { p.Close() })
	s.p = p
	return s
}

func get(t *testing.T, c *http.Client, u string) (int, string, string) {
	t.Helper()
	resp, err := c.Get(u)
	if err != nil {
		return 0, err.Error(), ""
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(body), resp.Header.Get(BlockedHeader)
}

func TestPlainHTTPGoesThroughAndAddsNothing(t *testing.T) {
	s := newSetup(t, nativeConfig)
	code, body, _ := get(t, proxied(s.p), "http://public.test/page")
	if code != 200 || !strings.Contains(body, "public host=public.test path=/page") {
		t.Fatalf("%d %q", code, body)
	}
	if !strings.Contains(body, `xff=""`) {
		t.Fatalf("the proxy told the site the browser's address: %q", body)
	}
}

func TestHTTPSIsTunnelledEndToEnd(t *testing.T) {
	s := newSetup(t, nativeConfig)
	code, body, _ := get(t, proxied(s.p), "https://secure.test/x")
	if code != 200 || !strings.Contains(body, "secure host=secure.test") {
		t.Fatalf("%d %q", code, body)
	}
}

func TestRefusalsNeverReachTheDestination(t *testing.T) {
	s := newSetup(t, nativeConfig)
	c := proxied(s.p)
	for _, u := range []string{
		"http://private.test/", // a name that resolves into the LAN
		"http://10.0.0.1/",     // the LAN by address
		"http://169.254.169.254/latest/meta-data/iam/security-credentials/",
		"http://127.0.0.1:8765/api/sessions", // the agent's API
		"http://localhost:3200/v1/models",    // the key proxy
		"http://[::1]:47001/",                // a daemon's hook listener
		"http://172.20.1.5:8765/",            // the API through this machine's own LAN address
		"http://nowhere.test/",
		fmt.Sprintf("http://127.0.0.1:%d/", s.p.Port()), // the proxy itself
	} {
		code, body, reason := get(t, c, u)
		if code != http.StatusForbidden || reason == "" || !strings.Contains(body, "network wall") {
			t.Errorf("%s: %d %q %q", u, code, reason, body)
		}
	}
	// And the same over CONNECT, which is how https and WebSockets travel.
	for _, target := range []string{"private.test:443", "127.0.0.1:8765", "169.254.169.254:443", "[fd00:ec2::254]:80", "0.0.0.0:8765"} {
		code, reason := connect(t, s.p, target)
		if code != http.StatusForbidden || reason == "" {
			t.Errorf("CONNECT %s: %d %q", target, code, reason)
		}
	}
	if s.private.hits.Load() != 0 {
		t.Fatalf("the private fixture was reached %d times", s.private.hits.Load())
	}
}

// connect sends a raw CONNECT and returns the status and the wall's reason.
func connect(t *testing.T, p *Proxy, target string) (int, string) {
	t.Helper()
	conn, err := net.Dial("tcp", p.Addr())
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	fmt.Fprintf(conn, "CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n", target, target)
	resp, err := http.ReadResponse(bufio.NewReader(conn), &http.Request{Method: http.MethodConnect})
	if err != nil {
		t.Fatal(err)
	}
	return resp.StatusCode, resp.Header.Get(BlockedHeader)
}

func TestATunnelCarriesBothWaysLikeAWebSocket(t *testing.T) {
	s := newSetup(t, nativeConfig)
	conn, err := net.Dial("tcp", s.p.Addr())
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	// The first bytes of the tunnel ride in the same packet as the CONNECT, as a client in a hurry
	// sends them: they must not be lost in the proxy's buffer.
	fmt.Fprintf(conn, "CONNECT echo.test:7 HTTP/1.1\r\nHost: echo.test:7\r\n\r\nearly ")
	r := bufio.NewReader(conn)
	resp, err := http.ReadResponse(r, &http.Request{Method: http.MethodConnect})
	if err != nil || resp.StatusCode != 200 {
		t.Fatalf("%v %v", resp, err)
	}
	fmt.Fprint(conn, "and late")
	got := make([]byte, len("early and late"))
	conn.SetReadDeadline(time.Now().Add(5 * time.Second))
	if _, err := io.ReadFull(r, got); err != nil || string(got) != "early and late" {
		t.Fatalf("%q %v", got, err)
	}
	// Closing the browser's proxy ends its open tunnels.
	s.p.Close()
	conn.SetReadDeadline(time.Now().Add(5 * time.Second))
	if _, err := r.ReadByte(); err == nil {
		t.Fatal("a tunnel outlived its proxy")
	}
}

func TestTheProxyIsNotAWebServer(t *testing.T) {
	s := newSetup(t, nativeConfig)
	resp, err := http.Get("http://" + s.p.Addr() + "/")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("an origin-form request got %d", resp.StatusCode)
	}
	// Only http is forwarded in absolute form; anything else must come as a CONNECT.
	conn, _ := net.Dial("tcp", s.p.Addr())
	defer conn.Close()
	fmt.Fprint(conn, "GET ftp://public.test/ HTTP/1.1\r\nHost: public.test\r\n\r\n")
	r, err := http.ReadResponse(bufio.NewReader(conn), nil)
	if err != nil || r.StatusCode != http.StatusBadRequest {
		t.Fatalf("%v %v", r, err)
	}
}

// DNS rebinding at the proxy: each connection is judged on the answer it is dialled with. A name
// that answered with a public address and then answers with a LAN one is refused the second time,
// and nothing ever connects to the LAN address.
func TestRebindingIsJudgedOnTheAddressDialled(t *testing.T) {
	s := newSetup(t, nativeConfig)
	c := proxied(s.p)
	if code, _, _ := get(t, c, "http://rebind.test/"); code != 200 {
		t.Fatalf("first answer: %d", code)
	}
	// A connection already open to the public address stays that address: the proxy keeps it for
	// the next request, and nothing about it changed. The rebinding lands on the next connection.
	s.p.tr.CloseIdleConnections()
	s.res.set("rebind.test", "10.0.0.1")
	if code, _, reason := get(t, c, "http://rebind.test/"); code != http.StatusForbidden || reason != ReasonPrivate {
		t.Fatalf("second answer: %d %q", code, reason)
	}
	s.p.tr.CloseIdleConnections()
	s.res.set("rebind.test", "203.0.113.10", "10.0.0.1")
	if code, _, reason := get(t, c, "http://rebind.test/"); code != http.StatusForbidden || reason != ReasonPrivate {
		t.Fatalf("a mixed answer: %d %q", code, reason)
	}
	if s.private.hits.Load() != 0 {
		t.Fatal("the LAN fixture was reached")
	}
}

func TestAskedDestinationsOpenOnlyByGrant(t *testing.T) {
	s := newSetup(t, nativeConfig)
	local := newFixture(t, false, "operator's own app")
	port := local.addr().Port()
	c := proxied(s.p)
	target := fmt.Sprintf("http://127.0.0.1:%d/", port)
	if code, _, reason := get(t, c, target); code != http.StatusForbidden || reason != ReasonLoopback {
		t.Fatalf("before the grant: %d %q", code, reason)
	}
	s.p.Grant("127.0.0.1", int(port), time.Now().Add(time.Minute))
	if code, body, _ := get(t, c, target); code != 200 || !strings.Contains(body, "operator's own app") {
		t.Fatalf("after the grant: %d %q", code, body)
	}
}

func TestTheServicesRangeIsOpenOnThisMachine(t *testing.T) {
	service := newFixture(t, false, "a service the agent started")
	port := int(service.addr().Port())
	c := nativeConfig
	c.ServicesPorts = [][2]int{{port, port}}
	s := newSetup(t, c)
	if code, body, _ := get(t, proxied(s.p), fmt.Sprintf("http://localhost:%d/", port)); code != 200 || !strings.Contains(body, "a service the agent started") {
		t.Fatalf("%d %q", code, body)
	}
}

func TestEveryConnectionIsInTheEgressLog(t *testing.T) {
	s := newSetup(t, nativeConfig)
	c := proxied(s.p)
	get(t, c, "http://public.test/")
	get(t, c, "http://public.test/again")
	get(t, c, "http://private.test/")
	var hosts []string
	for _, e := range *s.events {
		if e.Browser != "b7" {
			t.Fatalf("%+v", e)
		}
		hosts = append(hosts, fmt.Sprintf("%s:%d/%s/%s", e.Host, e.Port, e.Decision, e.Reason))
	}
	want := "public.test:80/allow/ private.test:80/deny/private"
	if strings.Join(hosts, " ") != want {
		t.Fatalf("egress %v, want %s", hosts, want)
	}
}
