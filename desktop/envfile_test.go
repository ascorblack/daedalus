package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestMergeEnvReplacesInPlaceAndKeepsTheRest(t *testing.T) {
	existing := "# a comment\nUSD_PER_DAY=5\n\nMY_OWN=keep-me\nexport SEARXNG_SECRET=old\n"
	got := mergeEnv(existing, []envVar{
		{"USD_PER_DAY", "20"},
		{"SEARXNG_SECRET", "new"},
		{"API_PORT", "8765"},
	})
	want := "# a comment\nUSD_PER_DAY=20\n\nMY_OWN=keep-me\nSEARXNG_SECRET=new\nAPI_PORT=8765\n"
	if got != want {
		t.Fatalf("merged file is\n%q\nwant\n%q", got, want)
	}
}

func TestMergeEnvDoesNotMatchAKeyThatMerelyStartsTheSame(t *testing.T) {
	got := mergeEnv("OPENAI_API_KEY_OLD=x\n", []envVar{{"OPENAI_API_KEY", "new"}})
	if !strings.Contains(got, "OPENAI_API_KEY_OLD=x") || !strings.Contains(got, "OPENAI_API_KEY=new") {
		t.Fatalf("a prefix match ate a line: %q", got)
	}
}

func TestReadEnvUnquotesAndSkipsComments(t *testing.T) {
	env := readEnv("# c\nA=\"one\"\nB='two'\n export C=three \nbroken\n")
	for key, want := range map[string]string{"A": "one", "B": "two", "C": "three"} {
		if env[key] != want {
			t.Fatalf("%s = %q, want %q", key, env[key], want)
		}
	}
	if _, ok := env["broken"]; ok {
		t.Fatal("a line without = became a variable")
	}
}

func TestCleanStripsWhatAPastedKeyDragsAlong(t *testing.T) {
	if got := clean("  sk-abc\r\n"); got != "sk-abc" {
		t.Fatalf("clean = %q", got)
	}
}

func TestEnvUpdatesPointAtTheDataFolderAndLoopback(t *testing.T) {
	paths, err := NewPaths(filepath.Join(t.TempDir(), "data"))
	if err != nil {
		t.Fatal(err)
	}
	env := map[string]string{}
	for _, v := range envUpdates(paths, Setup{}, "secret", "/home/someone") {
		env[v.Key] = v.Value
	}
	if env["DAEDALUS_COMPOSE_PROJECT_DIR"] != paths.Bot || env["DAEDALUS_COMPOSE_FILE"] != paths.Compose || env["DAEDALUS_CORE_PROJECT_DIR"] != paths.Core {
		t.Fatalf("the rebuilder's paths are not the data folder's: %v", env)
	}
	if !filepath.IsAbs(env["DAEDALUS_COMPOSE_PROJECT_DIR"]) {
		t.Fatal("the rebuilder resolves its paths on the host: they must be absolute")
	}
	if env["SERVICES_PUBLIC_HOST"] != "127.0.0.1" {
		t.Fatalf("SERVICES_PUBLIC_HOST = %q", env["SERVICES_PUBLIC_HOST"])
	}
	if env["MINIAPP_PUBLIC_URL"] != "" {
		t.Fatalf("a desktop install has no public address: %q", env["MINIAPP_PUBLIC_URL"])
	}
	if env["USD_PER_DAY"] != "20" {
		t.Fatalf("the default cap is 20, got %q", env["USD_PER_DAY"])
	}
	if env["DAEDALUS_SECRETS_FILE"] != paths.KeyproxyEnv || env["DAEDALUS_SSH_DIR"] != paths.SSH {
		t.Fatalf("the secrets are not where the launcher put them: %v", env)
	}
	if env["DAEDALUS_HARNESS_HOME"] != "/home/someone" {
		t.Fatalf("the CLI logins are mounted from the home directory, got %q", env["DAEDALUS_HARNESS_HOME"])
	}
}

func TestWriteSetupKeepsProviderKeysOutOfTheCheckout(t *testing.T) {
	paths := setupTempInstall(t)
	setup := Setup{DeepseekKey: "sk-deepseek", OpenrouterKey: "sk-router", BotToken: "123:abc", OwnerID: "42", USDPerDay: "7"}
	if err := WriteSetup(paths, setup); err != nil {
		t.Fatal(err)
	}
	env := readFile(paths.Env)
	if strings.Contains(env, "sk-deepseek") || strings.Contains(env, "sk-router") {
		t.Fatal("a provider key reached .env, which is mounted into the agent container")
	}
	keys := readEnv(readFile(paths.KeyproxyEnv))
	if keys["DEEPSEEK_API_KEY"] != "sk-deepseek" || keys["KEYPROXY_USD_PER_DAY"] != "7" {
		t.Fatalf("the key proxy's file is wrong: %v", keys)
	}
	if info, err := os.Stat(paths.KeyproxyEnv); err != nil {
		t.Fatal(err)
	} else if info.Mode().Perm() != 0o600 {
		t.Fatalf("the secrets file is %v, want 0600", info.Mode().Perm())
	}
	if readFile(paths.BotEnv) != env {
		t.Fatal("the checkout's .env is not the one compose reads for the container")
	}
}

func TestWriteSetupKeepsTheSearxngSecretAndUntypedKeys(t *testing.T) {
	paths := setupTempInstall(t)
	if err := WriteSetup(paths, Setup{DeepseekKey: "sk-one"}); err != nil {
		t.Fatal(err)
	}
	first := readEnv(readFile(paths.Env))["SEARXNG_SECRET"]
	if len(first) < 32 {
		t.Fatalf("the SearXNG secret is too short to be one: %q", first)
	}
	if err := WriteSetup(paths, Setup{DeepseekKey: "sk-one"}); err != nil {
		t.Fatal(err)
	}
	if second := readEnv(readFile(paths.Env))["SEARXNG_SECRET"]; second != first {
		t.Fatal("re-running setup rotated the SearXNG secret")
	}
	if got := CurrentSetup(paths); got.DeepseekKey != "sk-one" {
		t.Fatalf("the setup page would open with an empty key field: %+v", got)
	}
}

func TestTelegramIsOffWithoutAToken(t *testing.T) {
	if (Setup{}).Telegram() {
		t.Fatal("no token, no Telegram")
	}
	if !(Setup{BotToken: "123:abc"}).Telegram() {
		t.Fatal("a token turns Telegram on")
	}
}

// setupTempInstall lays out an empty install with a checkout directory, which is what a cloned
// repository would give the launcher.
func setupTempInstall(t *testing.T) Paths {
	t.Helper()
	paths, err := NewPaths(filepath.Join(t.TempDir(), "data"))
	if err != nil {
		t.Fatal(err)
	}
	if err := paths.EnsureDirs(); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(paths.Bot, "deploy"), 0o755); err != nil {
		t.Fatal(err)
	}
	return paths
}
