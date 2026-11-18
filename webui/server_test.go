package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

const (
	herokuURL = "postgres://u9abc:p$ss'w%3Ard@ec2-1-2-3-4.compute-1.amazonaws.com:5432/d1abc"
	renderURL = "postgresql://render_user:s3cret-render@dpg-abc123-a.oregon-postgres.render.com/app_db"
)

// newTestServer builds a server over a temporary repo whose local/ scripts
// are stubs that echo their arguments (and fail on demand).
func newTestServer(t *testing.T) (*Server, string) {
	t.Helper()
	repo := t.TempDir()
	local := filepath.Join(repo, "local")
	if err := os.MkdirAll(local, 0o755); err != nil {
		t.Fatal(err)
	}
	stub := `#!/usr/bin/env bash
echo "stub $(basename "$0") $* MIGRATION=${MIGRATION:-}"
case "$*" in
  *preflight.sh*)
    echo "BLOCKER: tables without a primary key (Bucardo cannot replicate them): public.audit_log"
    echo "WARN:    HEROKU_DATABASE_URL has a ':' in its password"
    echo "INFO:    every table has a primary key"
    echo "RESULT: fail (1 blocker(s), 1 warning(s))"
    exit 1 ;;
  *sleep*) sleep 5 ;;
  *leak*) printf 'oops '; cat "$(dirname "$0")/leak.txt" ;;
esac
`
	for _, name := range []string{"run.sh", "create_sandbox.sh", "status.sh", "teardown.sh", "fetch_artifacts.sh", "snapshot.sh", "build_image.sh", "import_image.sh"} {
		if err := os.WriteFile(filepath.Join(local, name), []byte(stub), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(local, "leak.txt"), []byte(herokuURL+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(local, "config.sh"), []byte("IMAGE_REPO=x\nRENDER_MIN_VERSION=2.36.0\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	migs := filepath.Join(repo, "migrations")
	store := &Store{dir: migs}
	s := &Server{
		repoRoot: repo, store: store,
		jobs:         NewJobManager(repo, migs, filepath.Join(migs, ".global-jobs"), nil),
		poller:       &Poller{store: store, repoRoot: repo, interval: time.Hour, inflight: map[string]bool{}},
		allowedHosts: map[string]bool{"localhost": true, "127.0.0.1": true},
	}
	return s, repo
}

func do(t *testing.T, h http.Handler, method, path string, body any) *httptest.ResponseRecorder {
	t.Helper()
	var r *http.Request
	if body != nil {
		b, _ := json.Marshal(body)
		r = httptest.NewRequest(method, path, strings.NewReader(string(b)))
		r.Header.Set("Content-Type", "application/json")
		r.Header.Set("X-PGM-Request", "1")
	} else {
		r = httptest.NewRequest(method, path, nil)
	}
	r.Host = "127.0.0.1:7788"
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	return w
}

func TestValidate(t *testing.T) {
	good := Settings{HerokuURL: herokuURL, RenderURL: renderURL}
	if errs := good.Validate(true); len(errs) != 0 {
		t.Fatalf("good settings rejected: %v", errs)
	}
	cases := map[string]Settings{
		"heroku_url":        {HerokuURL: "mysql://a:b@h/d", RenderURL: renderURL},
		"render_url":        {HerokuURL: herokuURL, RenderURL: "postgresql://u:p@dpg-abc123-a/app_db"}, // internal URL
		"sync_name":         {HerokuURL: herokuURL, RenderURL: renderURL, SyncName: "1bad name"},
		"schemas":           {HerokuURL: herokuURL, RenderURL: renderURL, Schemas: "public;drop"},
		"plan":              {HerokuURL: herokuURL, RenderURL: renderURL, Plan: "huge"},
		"slack_webhook_url": {HerokuURL: herokuURL, RenderURL: renderURL, SlackWebhookURL: "http://x"},
	}
	for field, st := range cases {
		if errs := st.Validate(true); errs[field] == "" {
			t.Errorf("%s: expected a validation error, got %v", field, errs)
		}
	}
	if errs := (Settings{HerokuURL: "postgres://u@h/d", RenderURL: renderURL}).Validate(true); errs["heroku_url"] == "" {
		t.Error("URL without a password accepted")
	}
	if errs := (Settings{}).Validate(false); len(errs) != 0 {
		t.Errorf("empty update rejected: %v", errs)
	}
}

func TestSettingsRoundTripAndQuoting(t *testing.T) {
	st := &Store{dir: t.TempDir()}
	if err := st.Create("acme", "", Settings{HerokuURL: herokuURL, RenderURL: renderURL}); err != nil {
		t.Fatal(err)
	}
	env, _ := os.ReadFile(st.path("acme", "migration.env"))
	// Single-quoted (no $ expansion by the CLI's dotenv parser) and the
	// single quote percent-encoded.
	if !strings.Contains(string(env), "HEROKU_DATABASE_URL='postgres://u9abc:p$ss%27w%3Ard@") {
		t.Fatalf("env file not quoted as expected:\n%s", env)
	}
	if fi, _ := os.Stat(st.path("acme", "migration.env")); fi.Mode().Perm() != 0o600 {
		t.Fatalf("migration.env mode %v", fi.Mode().Perm())
	}
	got, err := st.Settings("acme")
	if err != nil {
		t.Fatal(err)
	}
	if got.SyncName != "migrate_acme" || got.Schemas != "public" || got.Plan != "2c-4g" {
		t.Fatalf("defaults: %+v", got)
	}
	if got.RenderURL != renderURL {
		t.Fatalf("render url round trip: %q", got.RenderURL)
	}
	if err := st.Create("acme", "", Settings{HerokuURL: herokuURL, RenderURL: renderURL}); err == nil {
		t.Fatal("duplicate create succeeded")
	}
	if err := st.Create("../x", "", Settings{}); err == nil {
		t.Fatal("path traversal id accepted")
	}
}

func TestSecretsAndRedact(t *testing.T) {
	st := &Store{dir: t.TempDir()}
	if err := st.Create("acme", "", Settings{HerokuURL: herokuURL, RenderURL: renderURL}); err != nil {
		t.Fatal(err)
	}
	secrets := st.Secrets("acme")
	text := "a p$ss%27w%3Ard b p$ss'w:rd c s3cret-render d " + renderURL
	out := Redact(text, secrets)
	for _, leak := range []string{"p$ss", "s3cret-render", "w:rd"} {
		if strings.Contains(out, leak) {
			t.Fatalf("leak %q in %q", leak, out)
		}
	}
	if s := Summarize(renderURL); s.Display != "postgresql://render_user@dpg-abc123-a.oregon-postgres.render.com/app_db" || strings.Contains(s.Display, "s3cret") {
		t.Fatalf("summary: %+v", s)
	}
}

func TestGuard(t *testing.T) {
	s, _ := newTestServer(t)
	h := s.routes()

	r := httptest.NewRequest("GET", "/api/migrations", nil)
	r.Host = "evil.example:7788" // DNS rebinding
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusMisdirectedRequest {
		t.Fatalf("foreign Host: %d", w.Code)
	}

	r = httptest.NewRequest("POST", "/api/migrations", strings.NewReader(`{}`))
	r.Host = "127.0.0.1:7788"
	r.Header.Set("Content-Type", "application/json")
	w = httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusForbidden {
		t.Fatalf("POST without X-PGM-Request: %d", w.Code)
	}

	r = httptest.NewRequest("POST", "/api/migrations", strings.NewReader(`{}`))
	r.Host = "127.0.0.1:7788"
	r.Header.Set("Content-Type", "application/json")
	r.Header.Set("X-PGM-Request", "1")
	r.Header.Set("Origin", "https://evil.example")
	w = httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusForbidden {
		t.Fatalf("cross-origin POST: %d", w.Code)
	}

	s.password = "pw"
	w = do(t, h, "GET", "/api/migrations", nil)
	if w.Code != http.StatusUnauthorized {
		t.Fatalf("missing basic auth: %d", w.Code)
	}
	if !strings.Contains(w.Header().Get("Content-Security-Policy"), "frame-ancestors 'none'") {
		t.Fatal("CSP missing")
	}
}

func TestAPIFlow(t *testing.T) {
	s, _ := newTestServer(t)
	h := s.routes()

	w := do(t, h, "POST", "/api/migrations", map[string]string{"id": "acme", "heroku_url": herokuURL, "render_url": renderURL})
	if w.Code != 201 {
		t.Fatalf("create: %d %s", w.Code, w.Body)
	}
	if strings.Contains(w.Body.String(), "s3cret") || strings.Contains(w.Body.String(), "p$ss") {
		t.Fatalf("create response leaks a password: %s", w.Body)
	}
	w = do(t, h, "POST", "/api/migrations", map[string]string{"id": "acme", "heroku_url": herokuURL, "render_url": renderURL})
	if w.Code != 400 {
		t.Fatalf("duplicate create: %d", w.Code)
	}

	// Destructive actions need the typed name.
	w = do(t, h, "POST", "/api/migrations/acme/actions/cutover", map[string]string{"confirm": "nope"})
	if w.Code != 400 {
		t.Fatalf("cutover without confirmation: %d", w.Code)
	}
	w = do(t, h, "POST", "/api/migrations/acme/actions/build_image", map[string]string{})
	if w.Code != 404 {
		t.Fatalf("global action on a migration: %d", w.Code)
	}

	// Preflight runs, fails, and its report is parsed.
	w = do(t, h, "POST", "/api/migrations/acme/actions/preflight", map[string]string{})
	if w.Code != 202 {
		t.Fatalf("preflight: %d %s", w.Code, w.Body)
	}
	var job Job
	_ = json.Unmarshal(w.Body.Bytes(), &job)
	var v MigrationView
	deadline := time.Now().Add(10 * time.Second)
	for {
		w = do(t, h, "GET", "/api/migrations/acme", nil)
		_ = json.Unmarshal(w.Body.Bytes(), &v)
		if v.Preflight != nil || time.Now().After(deadline) {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	if v.Preflight == nil || v.Preflight.Passed || len(v.Preflight.Blockers) != 1 || len(v.Preflight.Warnings) != 1 {
		t.Fatalf("preflight not parsed: %+v", v.Preflight)
	}
	w = do(t, h, "GET", "/api/migrations/acme/jobs/"+job.ID+"/log", nil)
	if !strings.Contains(w.Body.String(), "stub run.sh preflight.sh MIGRATION=acme") {
		t.Fatalf("log: %s", w.Body)
	}

	// A mutating job blocks a second one for the same migration.
	sleepAction := Action{Name: "sleepy", Label: "Sleepy", Script: "run.sh", Args: []string{"sleep"}}
	if _, err := s.jobs.Start(sleepAction, "acme", nil); err != nil {
		t.Fatal(err)
	}
	w = do(t, h, "POST", "/api/migrations/acme/actions/schema", map[string]string{})
	if w.Code != 409 {
		t.Fatalf("concurrent mutating job: %d", w.Code)
	}
	active := s.jobs.Active("acme")
	if active == nil {
		t.Fatal("no active job")
	}
	w = do(t, h, "POST", "/api/migrations/acme/jobs/"+active.ID+"/cancel", map[string]string{})
	if w.Code != 202 {
		t.Fatalf("cancel: %d %s", w.Code, w.Body)
	}
	for i := 0; i < 100 && s.jobs.Active("acme") != nil; i++ {
		time.Sleep(50 * time.Millisecond)
	}
	if s.jobs.Active("acme") != nil {
		t.Fatal("cancel did not stop the job")
	}

	// Logs are redacted even if a script misbehaves.
	leak := Action{Name: "leak", Label: "Leak", Script: "run.sh", Args: []string{"leak"}, ReadOnly: true}
	lj, _ := s.jobs.Start(leak, "acme", nil)
	time.Sleep(500 * time.Millisecond)
	w = do(t, h, "GET", "/api/migrations/acme/jobs/"+lj.ID+"/log", nil)
	if strings.Contains(w.Body.String(), "p$ss") || !strings.Contains(w.Body.String(), "oops ****") {
		t.Fatalf("log not redacted: %s", w.Body)
	}

	// Settings lock once a sandbox exists; delete needs teardown.
	_ = os.WriteFile(s.store.path("acme", ".sandbox_id"), []byte("sbx-1\n"), 0o644)
	w = do(t, h, "PATCH", "/api/migrations/acme", map[string]string{"schemas": "public,app"})
	if w.Code != 409 {
		t.Fatalf("settings edit with sandbox: %d", w.Code)
	}
	w = do(t, h, "PATCH", "/api/migrations/acme", map[string]string{"title": "Acme prod"})
	if w.Code != 200 || !strings.Contains(w.Body.String(), "Acme prod") {
		t.Fatalf("title edit: %d %s", w.Code, w.Body)
	}
	w = do(t, h, "DELETE", "/api/migrations/acme", map[string]string{"confirm": "acme"})
	if w.Code != 409 {
		t.Fatalf("delete with sandbox: %d", w.Code)
	}
	_ = os.Remove(s.store.path("acme", ".sandbox_id"))
	w = do(t, h, "DELETE", "/api/migrations/acme", map[string]string{"confirm": "acme"})
	if w.Code != 204 {
		t.Fatalf("delete: %d %s", w.Code, w.Body)
	}
}

func TestMarkInterrupted(t *testing.T) {
	dir := t.TempDir()
	jobs := filepath.Join(dir, "acme", "jobs")
	_ = os.MkdirAll(jobs, 0o700)
	j := Job{ID: "20260101T000000Z-abcdef", Action: "schema", State: "running", StartedAt: time.Now()}
	b, _ := json.Marshal(j)
	_ = os.WriteFile(filepath.Join(jobs, j.ID+".json"), b, 0o644)
	jm := NewJobManager(dir, dir, filepath.Join(dir, ".global-jobs"), nil)
	got, err := jm.Get("acme", j.ID)
	if err != nil || got.State != "interrupted" {
		t.Fatalf("got %+v %v", got, err)
	}
}
