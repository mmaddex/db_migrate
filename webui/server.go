package main

import (
	"bufio"
	"context"
	"crypto/subtle"
	"embed"
	"encoding/json"
	"errors"
	"io/fs"
	"log"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
)

//go:embed static
var staticFS embed.FS

type Server struct {
	repoRoot     string
	store        *Store
	jobs         *JobManager
	poller       *Poller
	password     string
	allowedHosts map[string]bool
	extraEnv     []string
	fontsDir     string

	sysMu    sync.Mutex
	sysCache map[string]any
	sysAt    time.Time
}

// ------------------------------------------------------------------ views

type MigrationView struct {
	Meta
	Source       ConnSummary       `json:"source"`
	Target       ConnSummary       `json:"target"`
	SyncName     string            `json:"sync_name"`
	Schemas      string            `json:"schemas"`
	Plan         string            `json:"plan"`
	HasSlack     bool              `json:"has_slack"`
	SandboxID    string            `json:"sandbox_id,omitempty"`
	SandboxInfo  map[string]string `json:"sandbox_info,omitempty"`
	TornDown     bool              `json:"torn_down"`
	CleanupLocal bool              `json:"cleanup_done_locally"`
	Status       *CachedStatus     `json:"status,omitempty"`
	ActiveJob    *Job              `json:"active_job,omitempty"`
	Jobs         []Job             `json:"jobs,omitempty"`
}

func (s *Server) view(id string, full bool) (MigrationView, error) {
	meta, err := s.store.Meta(id)
	if err != nil {
		return MigrationView{}, err
	}
	st, err := s.store.Settings(id)
	if err != nil {
		return MigrationView{}, err
	}
	v := MigrationView{
		Meta: meta, Source: Summarize(st.HerokuURL), Target: Summarize(st.RenderURL),
		SyncName: st.SyncName, Schemas: st.Schemas, Plan: st.Plan, HasSlack: st.SlackWebhookURL != "",
		SandboxID:    s.store.ReadFile(id, ".sandbox_id"),
		TornDown:     s.store.FileExists(id, "teardown.done"),
		CleanupLocal: s.store.FileExists(id, "cleanup_source.done"),
		ActiveJob:    s.jobs.Active(id),
	}
	if v.Plan == "" {
		v.Plan = "2c-4g"
	}
	if info := s.store.ReadKV(id, "sandbox.info"); len(info) > 0 {
		v.SandboxInfo = info
	}
	if cs, ok := s.poller.Cached(id); ok {
		v.Status = &cs
	}
	if full {
		v.Jobs = s.jobs.List(id, 25)
	}
	return v, nil
}

// ------------------------------------------------------------- handlers

func (s *Server) routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/system", s.handleSystem)
	mux.HandleFunc("GET /api/migrations", s.handleList)
	mux.HandleFunc("POST /api/migrations", s.handleCreate)
	mux.HandleFunc("GET /api/migrations/{id}", s.handleGet)
	mux.HandleFunc("PATCH /api/migrations/{id}", s.handleUpdate)
	mux.HandleFunc("DELETE /api/migrations/{id}", s.handleDelete)
	mux.HandleFunc("POST /api/migrations/{id}/refresh", s.handleRefresh)
	mux.HandleFunc("POST /api/migrations/{id}/actions/{action}", s.handleAction)
	mux.HandleFunc("GET /api/migrations/{id}/jobs/{job}/log", s.handleLog)
	mux.HandleFunc("POST /api/migrations/{id}/jobs/{job}/cancel", s.handleCancel)
	mux.HandleFunc("POST /api/global/actions/{action}", s.handleGlobalAction)
	mux.HandleFunc("GET /api/global/jobs", s.handleGlobalJobs)
	mux.HandleFunc("GET /api/global/jobs/{job}/log", s.handleGlobalLog)

	static, _ := fs.Sub(staticFS, "static")
	mux.Handle("GET /", http.FileServer(http.FS(static)))
	if s.fontsDir != "" {
		mux.Handle("GET /fonts/", http.StripPrefix("/fonts/", http.FileServer(http.Dir(s.fontsDir))))
	}
	return s.guard(mux)
}

func (s *Server) migrationID(w http.ResponseWriter, r *http.Request) (string, bool) {
	id := r.PathValue("id")
	if !idPattern.MatchString(id) || !s.store.Exists(id) {
		writeErr(w, http.StatusNotFound, "no such migration")
		return "", false
	}
	return id, true
}

func (s *Server) handleList(w http.ResponseWriter, r *http.Request) {
	ids, err := s.store.List()
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	out := []MigrationView{}
	for _, id := range ids {
		if v, err := s.view(id, false); err == nil {
			out = append(out, v)
		}
	}
	writeJSON(w, 200, out)
}

type createReq struct {
	ID    string `json:"id"`
	Title string `json:"title"`
	Settings
}

func (s *Server) handleCreate(w http.ResponseWriter, r *http.Request) {
	var req createReq
	if !readJSON(w, r, &req) {
		return
	}
	req.ID = strings.TrimSpace(req.ID)
	errs := req.Settings.Validate(true)
	if !idPattern.MatchString(req.ID) {
		errs["id"] = "1-40 lowercase letters, digits or dashes; start with a letter or digit"
	} else if s.store.Exists(req.ID) {
		errs["id"] = "a migration with this name already exists"
	}
	if len(errs) > 0 {
		writeJSON(w, 400, map[string]any{"error": "invalid input", "fields": errs})
		return
	}
	if err := s.store.Create(req.ID, strings.TrimSpace(req.Title), req.Settings); err != nil {
		writeErr(w, 400, err.Error())
		return
	}
	log.Printf("created migration %s", req.ID)
	v, _ := s.view(req.ID, true)
	writeJSON(w, 201, v)
}

func (s *Server) handleGet(w http.ResponseWriter, r *http.Request) {
	id, ok := s.migrationID(w, r)
	if !ok {
		return
	}
	v, err := s.view(id, true)
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	writeJSON(w, 200, v)
}

type updateReq struct {
	Title *string `json:"title"`
	Settings
}

// handleUpdate changes the title at any time, and connection settings only
// while no sandbox exists (a sandbox keeps the env it was created with).
func (s *Server) handleUpdate(w http.ResponseWriter, r *http.Request) {
	id, ok := s.migrationID(w, r)
	if !ok {
		return
	}
	var req updateReq
	if !readJSON(w, r, &req) {
		return
	}
	if req.Settings != (Settings{}) {
		if s.store.FileExists(id, ".sandbox_id") {
			writeErr(w, 409, "settings are locked while a sandbox exists: it keeps the values it was created with. Tear it down first.")
			return
		}
		if errs := req.Settings.Validate(false); len(errs) > 0 {
			writeJSON(w, 400, map[string]any{"error": "invalid input", "fields": errs})
			return
		}
		if err := s.store.UpdateSettings(id, req.Settings); err != nil {
			writeErr(w, 500, err.Error())
			return
		}
	}
	if req.Title != nil {
		m, _ := s.store.Meta(id)
		m.Title = strings.TrimSpace(*req.Title)
		if m.Title == "" {
			m.Title = id
		}
		_ = s.store.SaveMeta(m)
	}
	v, _ := s.view(id, true)
	writeJSON(w, 200, v)
}

type confirmReq struct {
	Confirm string `json:"confirm"`
}

func (s *Server) handleDelete(w http.ResponseWriter, r *http.Request) {
	id, ok := s.migrationID(w, r)
	if !ok {
		return
	}
	var req confirmReq
	if !readJSON(w, r, &req) {
		return
	}
	if req.Confirm != id {
		writeErr(w, 400, "type the migration name to confirm")
		return
	}
	if s.store.FileExists(id, ".sandbox_id") {
		writeErr(w, 409, "this migration still has a sandbox; tear it down first")
		return
	}
	if s.jobs.Active(id) != nil {
		writeErr(w, 409, "a job is running")
		return
	}
	if err := s.store.Delete(id); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	log.Printf("deleted migration %s", id)
	w.WriteHeader(204)
}

func (s *Server) handleRefresh(w http.ResponseWriter, r *http.Request) {
	id, ok := s.migrationID(w, r)
	if !ok {
		return
	}
	go s.poller.Refresh(id)
	writeJSON(w, 202, map[string]string{"status": "refreshing"})
}

func (s *Server) handleAction(w http.ResponseWriter, r *http.Request) {
	id, ok := s.migrationID(w, r)
	if !ok {
		return
	}
	a, found := findAction(r.PathValue("action"))
	if !found || a.Global {
		writeErr(w, 404, "no such action")
		return
	}
	var req confirmReq
	if !readJSON(w, r, &req) {
		return
	}
	if a.Destructive && req.Confirm != id {
		writeErr(w, 400, "type the migration name to confirm")
		return
	}
	job, err := s.jobs.Start(a, id, func(j *Job) { s.afterJob(id, j) })
	if errors.Is(err, errBusy) {
		writeJSON(w, 409, map[string]any{"error": err.Error(), "job": job})
		return
	}
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	log.Printf("migration %s: started %s (%s)", id, a.Name, job.ID)
	writeJSON(w, 202, job)
}

var preflightLine = regexp.MustCompile(`^(BLOCKER|WARN|INFO):\s+(.*)$`)

// afterJob records preflight results and refreshes status.
func (s *Server) afterJob(id string, j *Job) {
	if j.Action == "preflight" {
		text, _, err := s.jobs.ReadLog(id, j.ID, 0, 4<<20)
		if err == nil {
			p := &Preflight{At: time.Now().UTC(), Blockers: []string{}, Warnings: []string{}, Info: []string{}}
			sc := bufio.NewScanner(strings.NewReader(Redact(text, s.store.Secrets(id))))
			sawResult := false
			for sc.Scan() {
				line := sc.Text()
				if m := preflightLine.FindStringSubmatch(line); m != nil {
					switch m[1] {
					case "BLOCKER":
						p.Blockers = append(p.Blockers, m[2])
					case "WARN":
						p.Warnings = append(p.Warnings, m[2])
					default:
						p.Info = append(p.Info, m[2])
					}
				}
				if strings.HasPrefix(line, "RESULT: ") {
					sawResult = true
				}
			}
			p.Passed = j.State == "succeeded" && sawResult && len(p.Blockers) == 0
			if sawResult {
				m, _ := s.store.Meta(id)
				m.Preflight = p
				_ = s.store.SaveMeta(m)
			}
		}
	}
	go s.poller.Refresh(id)
}

func (s *Server) handleLog(w http.ResponseWriter, r *http.Request) {
	id, ok := s.migrationID(w, r)
	if !ok {
		return
	}
	s.serveLog(w, r, id, s.store.Secrets(id))
}

func (s *Server) handleGlobalLog(w http.ResponseWriter, r *http.Request) {
	s.serveLog(w, r, "", nil)
}

func (s *Server) serveLog(w http.ResponseWriter, r *http.Request, migration string, secrets []string) {
	jobID := r.PathValue("job")
	job, err := s.jobs.Get(migration, jobID)
	if err != nil {
		writeErr(w, 404, "no such job")
		return
	}
	offset, _ := strconv.ParseInt(r.URL.Query().Get("offset"), 10, 64)
	text, next, err := s.jobs.ReadLog(migration, jobID, offset, 256<<10)
	if err != nil {
		writeErr(w, 404, "no log")
		return
	}
	// Defence in depth: the scripts never print secrets, but redact anyway.
	writeJSON(w, 200, map[string]any{"job": job, "text": Redact(text, secrets), "next_offset": next})
}

func (s *Server) handleCancel(w http.ResponseWriter, r *http.Request) {
	id, ok := s.migrationID(w, r)
	if !ok {
		return
	}
	if err := s.jobs.Cancel(id, r.PathValue("job")); err != nil {
		writeErr(w, 409, err.Error())
		return
	}
	writeJSON(w, 202, map[string]string{"status": "cancelling"})
}

func (s *Server) handleGlobalAction(w http.ResponseWriter, r *http.Request) {
	a, found := findAction(r.PathValue("action"))
	if !found || !a.Global {
		writeErr(w, 404, "no such action")
		return
	}
	var req confirmReq
	if !readJSON(w, r, &req) {
		return
	}
	job, err := s.jobs.Start(a, "", func(*Job) { s.invalidateSystem() })
	if errors.Is(err, errBusy) {
		writeJSON(w, 409, map[string]any{"error": err.Error(), "job": job})
		return
	}
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	log.Printf("started global %s (%s)", a.Name, job.ID)
	writeJSON(w, 202, job)
}

func (s *Server) handleGlobalJobs(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, 200, map[string]any{"jobs": s.jobs.List("", 15), "active": s.jobs.Active("")})
}

// handleSystem reports the operator environment: effective config from
// local/config.sh, the Render CLI, Docker and the imported images.
func (s *Server) handleSystem(w http.ResponseWriter, r *http.Request) {
	s.sysMu.Lock()
	if s.sysCache == nil || time.Since(s.sysAt) > 30*time.Second || r.URL.Query().Get("fresh") == "1" {
		s.sysCache = s.collectSystem()
		s.sysAt = time.Now()
	}
	out := map[string]any{}
	for k, v := range s.sysCache {
		out[k] = v
	}
	s.sysMu.Unlock()
	out["active_global_job"] = s.jobs.Active("")
	writeJSON(w, 200, out)
}

func (s *Server) invalidateSystem() {
	s.sysMu.Lock()
	s.sysCache = nil
	s.sysMu.Unlock()
}

func (s *Server) collectSystem() map[string]any {
	run := func(name string, args ...string) (string, error) {
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		defer cancel()
		cmd := exec.CommandContext(ctx, name, args...)
		cmd.Dir = s.repoRoot
		cmd.Env = append(append(os.Environ(), s.extraEnv...), "MIGRATIONS_DIR="+s.store.dir)
		out, err := cmd.Output()
		return strings.TrimSpace(string(out)), err
	}
	cfg := map[string]string{}
	out, err := run("bash", "-c", `set -e; . local/config.sh; for v in IMAGE_REPO IMAGE_PLATFORM PG_MAJOR IMAGE_NAME SANDBOX_GROUP SANDBOX_PLAN RENDER_MIN_VERSION LOCAL_STATE; do printf '%s=%s\n' "$v" "${!v}"; done`)
	if err == nil {
		for _, line := range strings.Split(out, "\n") {
			if k, v, ok := strings.Cut(line, "="); ok {
				cfg[k] = v
			}
		}
	}
	sys := map[string]any{"repo_root": s.repoRoot, "migrations_dir": s.store.dir, "config": cfg, "plans": plans}
	acts := []Action{}
	for _, a := range actions {
		if !a.Global {
			acts = append(acts, a)
		}
	}
	sys["actions"] = acts

	sha, _ := run("git", "rev-parse", "--short=12", "HEAD")
	dirty, _ := run("git", "status", "--porcelain")
	sys["git"] = map[string]any{"sha": sha, "dirty": dirty != ""}

	// Resolve tools through bash so a prepended PATH (tests) applies.
	cli := map[string]any{"found": false}
	if v, err := run("bash", "-c", "command -v render >/dev/null && render --version"); err == nil {
		cli["found"] = true
		if m := regexp.MustCompile(`\d+\.\d+\.\d+`).FindString(v); m != "" {
			cli["version"] = m
			cli["ok"] = versionGE(m, cfg["RENDER_MIN_VERSION"])
		}
	}
	sys["render_cli"] = cli
	_, derr := run("bash", "-c", "command -v docker")
	sys["docker"] = derr == nil

	images := map[string]string{}
	stateDir := cfg["LOCAL_STATE"]
	if stateDir == "" {
		stateDir = filepath.Join(s.repoRoot, ".state")
	}
	files, _ := filepath.Glob(filepath.Join(stateDir, "images", "*.id"))
	for _, f := range files {
		b, _ := os.ReadFile(f)
		images[strings.TrimSuffix(filepath.Base(f), ".id")] = strings.TrimSpace(string(b))
	}
	latest, _ := os.ReadFile(filepath.Join(stateDir, "images", "latest"))
	built, _ := filepath.Glob(filepath.Join(stateDir, "images", "*.built"))
	builtTags := []string{}
	for _, f := range built {
		builtTags = append(builtTags, strings.TrimSuffix(filepath.Base(f), ".built"))
	}
	sys["images"] = map[string]any{"imported": images, "latest": strings.TrimSpace(string(latest)), "built": builtTags}
	return sys
}

func versionGE(a, b string) bool {
	if b == "" {
		return true
	}
	pa, pb := strings.Split(a, "."), strings.Split(b, ".")
	for i := 0; i < 3; i++ {
		var x, y int
		if i < len(pa) {
			x, _ = strconv.Atoi(pa[i])
		}
		if i < len(pb) {
			y, _ = strconv.Atoi(pb[i])
		}
		if x != y {
			return x > y
		}
	}
	return true
}

// --------------------------------------------------------------- security

// guard protects a tool that can read database credentials and run
// commands on this machine:
//   - Host must be an allowed name (stops DNS rebinding).
//   - Optional HTTP basic auth (required when not bound to loopback).
//   - State-changing requests must send JSON with X-PGM-Request: 1 and, if
//     present, a same-origin Origin. Browsers cannot send that header
//     cross-origin without a CORS preflight, which this server never grants.
func (s *Server) guard(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h := w.Header()
		h.Set("X-Content-Type-Options", "nosniff")
		h.Set("Referrer-Policy", "no-referrer")
		h.Set("X-Frame-Options", "DENY")
		h.Set("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
		h.Set("Cache-Control", "no-store")

		host := r.Host
		if hh, _, err := net.SplitHostPort(host); err == nil {
			host = hh
		}
		host = strings.Trim(host, "[]")
		if !s.allowedHosts[strings.ToLower(host)] {
			http.Error(w, "host not allowed", http.StatusMisdirectedRequest)
			return
		}
		if s.password != "" {
			_, pass, ok := r.BasicAuth()
			if !ok || subtle.ConstantTimeCompare([]byte(pass), []byte(s.password)) != 1 {
				h.Set("WWW-Authenticate", `Basic realm="pg-migrate", charset="UTF-8"`)
				http.Error(w, "authentication required", http.StatusUnauthorized)
				return
			}
		}
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			if r.Header.Get("X-PGM-Request") != "1" || !strings.HasPrefix(r.Header.Get("Content-Type"), "application/json") {
				writeErr(w, http.StatusForbidden, "missing request header")
				return
			}
			if o := r.Header.Get("Origin"); o != "" && o != "http://"+r.Host && o != "https://"+r.Host {
				writeErr(w, http.StatusForbidden, "cross-origin request refused")
				return
			}
		}
		next.ServeHTTP(w, r)
	})
}

// ---------------------------------------------------------------- helpers

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, map[string]string{"error": msg})
}

func readJSON(w http.ResponseWriter, r *http.Request, v any) bool {
	r.Body = http.MaxBytesReader(w, r.Body, 64<<10)
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		writeErr(w, 400, "invalid JSON: "+err.Error())
		return false
	}
	return true
}
