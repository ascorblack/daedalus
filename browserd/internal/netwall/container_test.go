package netwall

import (
	"context"
	"fmt"
	"net"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"
)

// TestContainerWall runs inside the `browser` compose service of a throwaway stack, with the real
// resolver, the real Docker networks and the image's full Chromium, and checks the two walls
// together: the network the service is on, and the proxy. It is skipped anywhere else.
//
//	BROWSERD_TEST_IN_CONTAINER=1           run at all
//	BROWSERD_TEST_CHROMIUM                 the Chromium to drive
//	WALL_SERVICES_PORT                     a port in the services ranges a stand-in service is
//	                                       published on, on the Docker host
//	WALL_KEYS_ADDR                         the key proxy's address on its own network (ip:port):
//	                                       it must not be reachable at all from here
//	WALL_PUBLIC_URL                        optional: a page on the internet the browser may open
func TestContainerWall(t *testing.T) {
	if os.Getenv("BROWSERD_TEST_IN_CONTAINER") != "1" {
		t.Skip("not inside a browser service")
	}
	servicesPort, _ := strconv.Atoi(os.Getenv("WALL_SERVICES_PORT"))
	keys := os.Getenv("WALL_KEYS_ADDR")
	if servicesPort == 0 || keys == "" {
		t.Fatal("WALL_SERVICES_PORT and WALL_KEYS_ADDR are required")
	}

	t.Run("the service's network has no route to the key proxy", func(t *testing.T) {
		// Without any proxy: the container's own network is the first wall.
		conn, err := net.DialTimeout("tcp", keys, 3*time.Second)
		if err == nil {
			conn.Close()
			t.Fatalf("%s answered from the browser's network", keys)
		}
		if addrs, err := net.DefaultResolver.LookupHost(context.Background(), "keyproxy"); err == nil {
			t.Fatalf("keyproxy resolves from the browser's network: %v", addrs)
		}
	})

	var events []Egress
	w := New(Options{Events: func(e Egress) { events = append(events, e) }})
	if err := w.Configure(Config{SealedPorts: []int{8765}, ServicesPorts: [][2]int{{servicesPort, servicesPort}}, LoopbackRewrite: "host.docker.internal"}); err != nil {
		t.Fatal(err)
	}
	proxy, err := w.Listen("container")
	if err != nil {
		t.Fatal(err)
	}
	defer proxy.Close()
	b := startChromium(t, os.Getenv("BROWSERD_TEST_CHROMIUM"), ChromiumArgs(proxy.Addr())...)
	b.openPage()
	var text string

	t.Run("Chromium's sandbox is on", func(t *testing.T) {
		b.navigate("chrome://sandbox")
		b.mustEval("document.body.innerText", &text)
		if !strings.Contains(text, "adequately sandboxed") {
			t.Fatalf("chrome://sandbox says:\n%s", text)
		}
	})

	t.Run("the agent's link to a service opens the service on the Docker host", func(t *testing.T) {
		b.navigate(fmt.Sprintf("http://127.0.0.1:%d/", servicesPort))
		b.mustEval("document.body.innerText", &text)
		if strings.Contains(text, "network wall") || !strings.Contains(text, "stand-in service") {
			t.Fatalf("the services range: %q", text)
		}
	})

	t.Run("the key proxy, the Docker host's other ports and metadata are refused", func(t *testing.T) {
		for _, u := range []string{"http://keyproxy:3200/v1/models", "http://" + keys + "/v1/models", "http://host.docker.internal:8765/api/sessions", "http://host.docker.internal:3000/", "http://169.254.169.254/latest/meta-data/"} {
			b.navigate(u)
			b.mustEval("document.body.innerText", &text)
			if !strings.Contains(text, "network wall") {
				t.Errorf("%s: %q", u, text)
			}
		}
	})

	if public := os.Getenv("WALL_PUBLIC_URL"); public != "" {
		t.Run("the internet is open", func(t *testing.T) {
			b.navigate(public)
			var title string
			b.mustEval("document.title", &title)
			if title == "" || strings.Contains(title, "network wall") {
				t.Fatalf("%s: title %q", public, title)
			}
		})
	}
	for _, e := range events {
		t.Logf("egress %s:%d %s %s", e.Host, e.Port, e.Decision, e.Reason)
	}
}
