package main

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"
)

// Store keeps migrations on disk in the same layout the local/ scripts use,
// so the CLI and the UI can drive the same migration:
//
//	<migrations dir>/<id>/migration.env   secrets for --env-file (0600)
//	<migrations dir>/<id>/migration.conf  non-secret script settings
//	<migrations dir>/<id>/ui.json         UI metadata (title, preflight result)
//	<migrations dir>/<id>/status.json     last `local/status.sh --json` output
//	<migrations dir>/<id>/jobs/           job records and logs
//	<migrations dir>/<id>/.sandbox_id     written by local/create_sandbox.sh
type Store struct {
	dir string
	mu  sync.Mutex // serialises writes to ui.json/env/conf files
}

var idPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,39}$`)
var syncNamePattern = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]{0,62}$`)
var schemasPattern = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_$]*(\s*,\s*[A-Za-z_][A-Za-z0-9_$]*)*$`)

var rawPasswordPattern = regexp.MustCompile(`^[A-Za-z]+://[^:/@]*:([^@]*)@`)

var plans = []string{"1c-2g", "2c-4g", "4c-8g", "8c-16g"}

// Meta is the UI's own record of a migration.
type Meta struct {
	ID        string     `json:"id"`
	Title     string     `json:"title"`
	CreatedAt time.Time  `json:"created_at"`
	Preflight *Preflight `json:"preflight,omitempty"`
}

// Preflight is parsed from preflight.sh's report lines.
type Preflight struct {
	At       time.Time `json:"at"`
	Passed   bool      `json:"passed"`
	Blockers []string  `json:"blockers"`
	Warnings []string  `json:"warnings"`
	Info     []string  `json:"info"`
}

// Settings are what the operator enters for a migration.
type Settings struct {
	HerokuURL       string `json:"heroku_url"`
	RenderURL       string `json:"render_url"`
	SyncName        string `json:"sync_name"`
	Schemas         string `json:"schemas"`
	Plan            string `json:"plan"`
	SlackWebhookURL string `json:"slack_webhook_url"`
}

// ConnSummary is a connection string with the password removed.
type ConnSummary struct {
	Display string `json:"display"`
	Host    string `json:"host"`
	DB      string `json:"db"`
}

func (s *Store) path(id string, parts ...string) string {
	return filepath.Join(append([]string{s.dir, id}, parts...)...)
}

func (s *Store) Exists(id string) bool {
	st, err := os.Stat(s.path(id))
	return err == nil && st.IsDir()
}

// List returns the IDs of all migrations, sorted.
func (s *Store) List() ([]string, error) {
	entries, err := os.ReadDir(s.dir)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var ids []string
	for _, e := range entries {
		if e.IsDir() && idPattern.MatchString(e.Name()) {
			ids = append(ids, e.Name())
		}
	}
	sort.Strings(ids)
	return ids, nil
}

func (s *Store) Meta(id string) (Meta, error) {
	m := Meta{ID: id, Title: id}
	b, err := os.ReadFile(s.path(id, "ui.json"))
	if errors.Is(err, os.ErrNotExist) {
		if st, serr := os.Stat(s.path(id)); serr == nil {
			m.CreatedAt = st.ModTime()
		}
		return m, nil
	}
	if err != nil {
		return m, err
	}
	err = json.Unmarshal(b, &m)
	m.ID = id
	return m, err
}

func (s *Store) SaveMeta(m Meta) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	b, err := json.MarshalIndent(m, "", "  ")
	if err != nil {
		return err
	}
	return writeFileAtomic(s.path(m.ID, "ui.json"), b, 0o644)
}

// Validate checks settings. With requireURLs false (an update), an empty URL
// means "leave the stored one unchanged".
func (st Settings) Validate(requireURLs bool) map[string]string {
	errs := map[string]string{}
	check := func(field, raw string) {
		if raw == "" {
			if requireURLs {
				errs[field] = "required"
			}
			return
		}
		if msg := validatePostgresURL(raw); msg != "" {
			errs[field] = msg
		}
	}
	check("heroku_url", st.HerokuURL)
	check("render_url", st.RenderURL)
	if st.HerokuURL != "" && st.HerokuURL == st.RenderURL {
		errs["render_url"] = "must differ from the Heroku URL"
	}
	if h := hostOf(st.RenderURL); strings.HasPrefix(h, "dpg-") && !strings.Contains(h, ".") {
		errs["render_url"] = "this looks like Render's internal URL; the sandbox needs the external URL (host ends in .render.com)"
	}
	if st.SyncName != "" && !syncNamePattern.MatchString(st.SyncName) {
		errs["sync_name"] = "letters, digits and underscores; must not start with a digit"
	}
	if st.Schemas != "" && !schemasPattern.MatchString(st.Schemas) {
		errs["schemas"] = "comma-separated schema names"
	}
	if st.Plan != "" && !contains(plans, st.Plan) {
		errs["plan"] = "one of " + strings.Join(plans, ", ")
	}
	if st.SlackWebhookURL != "" && !strings.HasPrefix(st.SlackWebhookURL, "https://") {
		errs["slack_webhook_url"] = "must be an https:// URL"
	}
	for k, v := range map[string]string{"heroku_url": st.HerokuURL, "render_url": st.RenderURL, "slack_webhook_url": st.SlackWebhookURL} {
		if strings.ContainsAny(v, "\r\n\t ") {
			errs[k] = "must not contain whitespace"
		}
	}
	return errs
}

func validatePostgresURL(raw string) string {
	u, err := url.Parse(raw)
	if err != nil {
		return "not a valid URL"
	}
	if u.Scheme != "postgres" && u.Scheme != "postgresql" {
		return "must start with postgres:// or postgresql://"
	}
	if u.Hostname() == "" {
		return "missing host"
	}
	if strings.Trim(u.Path, "/") == "" {
		return "missing database name"
	}
	if u.User == nil || u.User.Username() == "" {
		return "missing user"
	}
	if _, ok := u.User.Password(); !ok {
		return "missing password"
	}
	return ""
}

func hostOf(raw string) string {
	u, err := url.Parse(raw)
	if err != nil {
		return ""
	}
	return u.Hostname()
}

// Summarize returns user@host:port/db without the password.
func Summarize(raw string) ConnSummary {
	u, err := url.Parse(raw)
	if err != nil || raw == "" {
		return ConnSummary{}
	}
	user := ""
	if u.User != nil {
		user = u.User.Username() + "@"
	}
	port := ""
	if u.Port() != "" {
		port = ":" + u.Port()
	}
	db := strings.Trim(u.Path, "/")
	return ConnSummary{Display: fmt.Sprintf("%s://%s%s%s/%s", u.Scheme, user, u.Hostname(), port, db), Host: u.Hostname(), DB: db}
}

// Create writes a new migration. It fails if the ID is taken.
func (s *Store) Create(id, title string, st Settings) error {
	if !idPattern.MatchString(id) {
		return fmt.Errorf("id must be 1-40 lowercase letters, digits or dashes, starting with a letter or digit")
	}
	if err := os.MkdirAll(s.dir, 0o700); err != nil {
		return err
	}
	if err := os.Mkdir(s.path(id), 0o700); err != nil {
		if errors.Is(err, os.ErrExist) {
			return fmt.Errorf("a migration named %q already exists", id)
		}
		return err
	}
	if st.SyncName == "" {
		st.SyncName = "migrate_" + strings.ReplaceAll(id, "-", "_")
	}
	if st.Schemas == "" {
		st.Schemas = "public"
	}
	if st.Plan == "" {
		st.Plan = "2c-4g"
	}
	if err := s.writeSettings(id, st); err != nil {
		return err
	}
	if title == "" {
		title = id
	}
	return s.SaveMeta(Meta{ID: id, Title: title, CreatedAt: time.Now().UTC()})
}

// UpdateSettings merges non-empty fields into the stored settings.
func (s *Store) UpdateSettings(id string, upd Settings) error {
	cur, err := s.Settings(id)
	if err != nil {
		return err
	}
	if upd.HerokuURL != "" {
		cur.HerokuURL = upd.HerokuURL
	}
	if upd.RenderURL != "" {
		cur.RenderURL = upd.RenderURL
	}
	if upd.SyncName != "" {
		cur.SyncName = upd.SyncName
	}
	if upd.Schemas != "" {
		cur.Schemas = upd.Schemas
	}
	if upd.Plan != "" {
		cur.Plan = upd.Plan
	}
	if upd.SlackWebhookURL != "" {
		cur.SlackWebhookURL = upd.SlackWebhookURL
	}
	return s.writeSettings(id, cur)
}

func (s *Store) writeSettings(id string, st Settings) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	var env strings.Builder
	env.WriteString("# Written by the pg-migrate web UI. Passed to the sandbox with --env-file.\n")
	env.WriteString("# Single-quoted so the Render CLI's env parser does not expand '$'.\n")
	writeKV(&env, "HEROKU_DATABASE_URL", st.HerokuURL)
	writeKV(&env, "RENDER_DATABASE_URL", st.RenderURL)
	writeKV(&env, "SYNC_NAME", st.SyncName)
	writeKV(&env, "SCHEMAS", strings.ReplaceAll(st.Schemas, " ", ""))
	if st.SlackWebhookURL != "" {
		writeKV(&env, "SLACK_WEBHOOK_URL", st.SlackWebhookURL)
	}
	if err := writeFileAtomic(s.path(id, "migration.env"), []byte(env.String()), 0o600); err != nil {
		return err
	}
	conf := fmt.Sprintf("# Written by the pg-migrate web UI.\nSANDBOX_PLAN=%s\n", st.Plan)
	return writeFileAtomic(s.path(id, "migration.conf"), []byte(conf), 0o644)
}

// writeKV writes KEY='value'. A single quote inside a URL is percent-encoded
// (%27), which libpq decodes back, so the value never needs escaping.
func writeKV(b *strings.Builder, key, value string) {
	value = strings.ReplaceAll(value, "'", "%27")
	fmt.Fprintf(b, "%s='%s'\n", key, value)
}

// Settings reads migration.env and migration.conf back.
func (s *Store) Settings(id string) (Settings, error) {
	env, err := readKV(s.path(id, "migration.env"))
	if err != nil {
		return Settings{}, err
	}
	conf, _ := readKV(s.path(id, "migration.conf"))
	return Settings{
		HerokuURL:       env["HEROKU_DATABASE_URL"],
		RenderURL:       env["RENDER_DATABASE_URL"],
		SyncName:        env["SYNC_NAME"],
		Schemas:         env["SCHEMAS"],
		SlackWebhookURL: env["SLACK_WEBHOOK_URL"],
		Plan:            conf["SANDBOX_PLAN"],
	}, nil
}

func readKV(path string) (map[string]string, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	out := map[string]string{}
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		line = strings.TrimPrefix(line, "export ")
		k, v, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		if len(v) >= 2 && (v[0] == '\'' || v[0] == '"') && v[len(v)-1] == v[0] {
			v = v[1 : len(v)-1]
		}
		out[k] = v
	}
	return out, sc.Err()
}

// Secrets returns strings that must never reach the browser for this
// migration: full URLs and their passwords (raw and decoded).
func (s *Store) Secrets(id string) []string {
	st, err := s.Settings(id)
	if err != nil {
		return nil
	}
	var out []string
	for _, raw := range []string{st.HerokuURL, st.RenderURL, st.SlackWebhookURL} {
		if raw == "" {
			continue
		}
		out = append(out, raw)
		if u, err := url.Parse(raw); err == nil && u.User != nil {
			if p, ok := u.User.Password(); ok && p != "" {
				out = append(out, p)
			}
		}
		// The password as written in the URL (possibly percent-encoded).
		if m := rawPasswordPattern.FindStringSubmatch(raw); m != nil && m[1] != "" {
			out = append(out, m[1])
		}
		// writeKV stores ' as %27; also catch the form the operator typed.
		if strings.Contains(raw, "%27") {
			typed := strings.ReplaceAll(raw, "%27", "'")
			out = append(out, typed)
			if m := rawPasswordPattern.FindStringSubmatch(typed); m != nil && m[1] != "" {
				out = append(out, m[1])
			}
		}
	}
	sort.Slice(out, func(i, j int) bool { return len(out[i]) > len(out[j]) })
	return out
}

func Redact(text string, secrets []string) string {
	for _, sec := range secrets {
		if len(sec) >= 3 {
			text = strings.ReplaceAll(text, sec, "****")
		}
	}
	return text
}

func (s *Store) ReadFile(id, name string) string {
	b, err := os.ReadFile(s.path(id, name))
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(b))
}

func (s *Store) FileExists(id, name string) bool {
	_, err := os.Stat(s.path(id, name))
	return err == nil
}

// ReadKV exposes key=value files like sandbox.info.
func (s *Store) ReadKV(id, name string) map[string]string {
	m, _ := readKV(s.path(id, name))
	return m
}

// Delete removes a migration directory. Callers check that no sandbox exists.
func (s *Store) Delete(id string) error {
	if !idPattern.MatchString(id) {
		return fmt.Errorf("bad id")
	}
	return os.RemoveAll(s.path(id))
}

func writeFileAtomic(path string, data []byte, mode os.FileMode) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(path), ".tmp-*")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	if err := tmp.Chmod(mode); err != nil {
		tmp.Close()
		return err
	}
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmp.Name(), path)
}

func contains(list []string, v string) bool {
	for _, x := range list {
		if x == v {
			return true
		}
	}
	return false
}
