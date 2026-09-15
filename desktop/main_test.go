package main

import "testing"

func TestParseArgsTakesTheCommandFromAnywhere(t *testing.T) {
	opts, err := parseArgs([]string{"logs", "-f"})
	if err != nil || opts.command != "logs" || !opts.follow {
		t.Fatalf("%+v %v", opts, err)
	}
	opts, err = parseArgs([]string{"--data", "/tmp/x", "status"})
	if err != nil || opts.command != "status" || opts.data != "/tmp/x" {
		t.Fatalf("%+v %v", opts, err)
	}
	opts, err = parseArgs([]string{"--port=9000", "--setup"})
	if err != nil || opts.port != 9000 || !opts.setup || opts.command != "" {
		t.Fatalf("%+v %v", opts, err)
	}
	if opts, _ := parseArgs(nil); opts.port != defaultPort {
		t.Fatalf("the default port is %d, got %d", defaultPort, opts.port)
	}
}

func TestParseArgsRefusesWhatItCannotMean(t *testing.T) {
	if _, err := parseArgs([]string{"--nonsense"}); err == nil {
		t.Fatal("an unknown flag is an error")
	}
	if _, err := parseArgs([]string{"start", "stop"}); err == nil {
		t.Fatal("two commands are an error")
	}
	if _, err := parseArgs([]string{"--port"}); err == nil {
		t.Fatal("a flag without its value is an error")
	}
	if _, err := parseArgs([]string{"--port", "abc"}); err == nil {
		t.Fatal("a port must be a number")
	}
	// A mistyped port that quietly became another one would leave the page somewhere unexpected.
	if _, err := parseArgs([]string{"--port", "8770abc"}); err == nil {
		t.Fatal("a port with something after it is an error")
	}
	if _, err := parseArgs([]string{"--port", "70000"}); err == nil {
		t.Fatal("a number that is not a port is an error")
	}
}
