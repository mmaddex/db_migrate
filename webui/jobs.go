package main

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"syscall"
	"time"
)

// Action is one operator step the UI can run. Each maps to a local/ script.
type Action struct {
	Name        string   `json:"name"`
	Label       string   `json:"label"`
	Script      string   `json:"-"` // path under local/
	Args        []string `json:"-"`
	Destructive bool     `json:"destructive"` // needs a typed confirmation
	Global      bool     `json:"-"`           // not tied to one migration
	ReadOnly    bool     `json:"read_only"`   // may run alongside other jobs
}

var actions = []Action{
	{Name: "build_image", Label: "Build & push image", Script: "build_image.sh", Global: true},
	{Name: "import_image", Label: "Import image", Script: "import_image.sh", Global: true},

	{Name: "create_sandbox", Label: "Create sandbox", Script: "create_sandbox.sh"},
	{Name: "preflight", Label: "Run preflight", Script: "run.sh", Args: []string{"preflight.sh"}, ReadOnly: true},
	{Name: "schema", Label: "Load schema", Script: "run.sh", Args: []string{"schema.sh"}},
	{Name: "start_sync", Label: "Start sync", Script: "run.sh", Args: []string{"start_sync.sh"}},
	{Name: "build_indexes", Label: "Build indexes", Script: "run.sh", Args: []string{"--detach", "build_indexes.sh"}},
	{Name: "status_exact", Label: "Exact row counts", Script: "status.sh", Args: []string{"--exact"}, ReadOnly: true},
	{Name: "fetch_artifacts", Label: "Fetch artifacts", Script: "fetch_artifacts.sh", ReadOnly: true},
	{Name: "snapshot", Label: "Snapshot sandbox", Script: "snapshot.sh", ReadOnly: true},
	{Name: "cutover", Label: "Cut over", Script: "run.sh", Args: []string{"cutover.sh", "--confirm"}, Destructive: true},
	{Name: "cleanup_source", Label: "Clean up Heroku", Script: "run.sh", Args: []string{"cleanup_source.sh", "--confirm"}, Destructive: true},
	{Name: "reset_target", Label: "Reset Render target", Script: "run.sh", Args: []string{"reset_target.sh", "--confirm"}, Destructive: true},
	{Name: "teardown", Label: "Tear down sandbox", Script: "teardown.sh", Args: []string{"--confirm"}, Destructive: true},
}

func findAction(name string) (Action, bool) {
	for _, a := range actions {
		if a.Name == name {
			return a, true
		}
	}
	return Action{}, false
}

// Job is one run of an action. Its record and log live in the jobs
// directory of its migration (or the global jobs directory).
type Job struct {
	ID         string     `json:"id"`
	Migration  string     `json:"migration,omitempty"`
	Action     string     `json:"action"`
	Label      string     `json:"label"`
	Command    string     `json:"command"`
	State      string     `json:"state"` // running | succeeded | failed | interrupted | cancelled
	ExitCode   *int       `json:"exit_code,omitempty"`
	StartedAt  time.Time  `json:"started_at"`
	FinishedAt *time.Time `json:"finished_at,omitempty"`
	ReadOnly   bool       `json:"read_only"`
	cancelled  bool
	cmd        *exec.Cmd
	dir        string
	onFinish   func(*Job)
}

func (j *Job) logPath() string  { return filepath.Join(j.dir, j.ID+".log") }
func (j *Job) metaPath() string { return filepath.Join(j.dir, j.ID+".json") }

// JobManager starts jobs and enforces one mutating job per migration (and
// one global job) at a time.
type JobManager struct {
	repoRoot      string
	migrationsDir string
	globalDir     string
	extraEnv      []string

	mu      sync.Mutex
	running map[string]*Job // key: migration ID ("" for global) -> mutating job
	jobs    map[string]*Job // job ID -> running job
}

func NewJobManager(repoRoot, migrationsDir, globalDir string, extraEnv []string) *JobManager {
	jm := &JobManager{
		repoRoot: repoRoot, migrationsDir: migrationsDir, globalDir: globalDir, extraEnv: extraEnv,
		running: map[string]*Job{}, jobs: map[string]*Job{},
	}
	jm.markInterrupted()
	return jm
}

func (jm *JobManager) jobDir(migration string) string {
	if migration == "" {
		return jm.globalDir
	}
	return filepath.Join(jm.migrationsDir, migration, "jobs")
}

// markInterrupted fixes up records left "running" by a previous server.
func (jm *JobManager) markInterrupted() {
	dirs, _ := filepath.Glob(filepath.Join(jm.migrationsDir, "*", "jobs"))
	dirs = append(dirs, jm.globalDir)
	for _, d := range dirs {
		files, _ := filepath.Glob(filepath.Join(d, "*.json"))
		for _, f := range files {
			var j Job
			b, err := os.ReadFile(f)
			if err != nil || json.Unmarshal(b, &j) != nil || j.State != "running" {
				continue
			}
			j.State = "interrupted"
			now := time.Now().UTC()
			j.FinishedAt = &now
			if b, err := json.MarshalIndent(&j, "", "  "); err == nil {
				_ = writeFileAtomic(f, b, 0o644)
			}
		}
	}
}

var errBusy = errors.New("busy")

// Start launches an action for a migration ("" for global actions).
func (jm *JobManager) Start(a Action, migration string, onFinish func(*Job)) (*Job, error) {
	jm.mu.Lock()
	defer jm.mu.Unlock()
	if !a.ReadOnly {
		if cur := jm.running[migration]; cur != nil {
			return cur, fmt.Errorf("%w: %s is still running", errBusy, cur.Label)
		}
	}
	id := time.Now().UTC().Format("20060102T150405Z") + "-" + randHex(3)
	j := &Job{
		ID: id, Migration: migration, Action: a.Name, Label: a.Label, State: "running",
		StartedAt: time.Now().UTC(), ReadOnly: a.ReadOnly, dir: jm.jobDir(migration), onFinish: onFinish,
	}
	args := append([]string{filepath.Join(jm.repoRoot, "local", a.Script)}, a.Args...)
	j.Command = "local/" + a.Script + " " + strings.Join(a.Args, " ")
	if err := os.MkdirAll(j.dir, 0o700); err != nil {
		return nil, err
	}
	logf, err := os.OpenFile(j.logPath(), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return nil, err
	}
	fmt.Fprintf(logf, "$ %s\n", strings.TrimSpace(j.Command))

	cmd := exec.Command("bash", args...)
	cmd.Dir = jm.repoRoot
	cmd.Env = append(os.Environ(), jm.extraEnv...)
	cmd.Env = append(cmd.Env, "MIGRATIONS_DIR="+jm.migrationsDir, "NO_COLOR=1")
	if migration != "" {
		cmd.Env = append(cmd.Env, "MIGRATION="+migration)
	}
	cmd.Stdin = nil
	cmd.Stdout = logf
	cmd.Stderr = logf
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := cmd.Start(); err != nil {
		logf.Close()
		return nil, err
	}
	j.cmd = cmd
	jm.jobs[id] = j
	if !a.ReadOnly {
		jm.running[migration] = j
	}
	_ = j.save()

	go func() {
		err := cmd.Wait()
		code := 0
		if err != nil {
			code = -1
			var ee *exec.ExitError
			if errors.As(err, &ee) {
				code = ee.ExitCode()
			}
		}
		now := time.Now().UTC()
		jm.mu.Lock()
		j.ExitCode = &code
		j.FinishedAt = &now
		switch {
		case j.cancelled:
			j.State = "cancelled"
		case code == 0:
			j.State = "succeeded"
		default:
			j.State = "failed"
		}
		fmt.Fprintf(logf, "\n[%s] exit %d (%s)\n", now.Format(time.RFC3339), code, j.State)
		logf.Close()
		_ = j.save()
		delete(jm.jobs, id)
		if jm.running[migration] == j {
			delete(jm.running, migration)
		}
		jm.mu.Unlock()
		if j.onFinish != nil {
			j.onFinish(j)
		}
	}()
	return j, nil
}

// Cancel sends SIGTERM to the job's process group.
func (jm *JobManager) Cancel(migration, id string) error {
	jm.mu.Lock()
	defer jm.mu.Unlock()
	j := jm.jobs[id]
	if j == nil || j.Migration != migration {
		return errors.New("job is not running")
	}
	j.cancelled = true
	return syscall.Kill(-j.cmd.Process.Pid, syscall.SIGTERM)
}

// Active returns the running mutating job for a migration, if any.
func (jm *JobManager) Active(migration string) *Job {
	jm.mu.Lock()
	defer jm.mu.Unlock()
	if j := jm.running[migration]; j != nil {
		c := *j
		return &c
	}
	return nil
}

func (j *Job) save() error {
	b, err := json.MarshalIndent(j, "", "  ")
	if err != nil {
		return err
	}
	return writeFileAtomic(j.metaPath(), b, 0o644)
}

// List returns the most recent jobs for a migration, newest first.
func (jm *JobManager) List(migration string, limit int) []Job {
	files, _ := filepath.Glob(filepath.Join(jm.jobDir(migration), "*.json"))
	sort.Sort(sort.Reverse(sort.StringSlice(files)))
	var out []Job
	for _, f := range files {
		if limit > 0 && len(out) >= limit {
			break
		}
		var j Job
		if b, err := os.ReadFile(f); err == nil && json.Unmarshal(b, &j) == nil {
			out = append(out, j)
		}
	}
	return out
}

func (jm *JobManager) Get(migration, id string) (Job, error) {
	var j Job
	if !jobIDPattern(id) {
		return j, errors.New("bad job id")
	}
	b, err := os.ReadFile(filepath.Join(jm.jobDir(migration), id+".json"))
	if err != nil {
		return j, err
	}
	return j, json.Unmarshal(b, &j)
}

// ReadLog returns log bytes from offset (at most max bytes) and the next offset.
func (jm *JobManager) ReadLog(migration, id string, offset int64, max int64) (string, int64, error) {
	if !jobIDPattern(id) {
		return "", 0, errors.New("bad job id")
	}
	f, err := os.Open(filepath.Join(jm.jobDir(migration), id+".log"))
	if err != nil {
		return "", 0, err
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil {
		return "", 0, err
	}
	if offset < 0 || offset > st.Size() {
		offset = 0
	}
	if _, err := f.Seek(offset, io.SeekStart); err != nil {
		return "", 0, err
	}
	buf := make([]byte, max)
	n, err := io.ReadFull(f, buf)
	if err != nil && !errors.Is(err, io.ErrUnexpectedEOF) && !errors.Is(err, io.EOF) {
		return "", 0, err
	}
	return string(buf[:n]), offset + int64(n), nil
}

func jobIDPattern(id string) bool {
	if len(id) < 10 || len(id) > 40 {
		return false
	}
	for _, c := range id {
		if !(c >= '0' && c <= '9' || c >= 'a' && c <= 'f' || c == 'T' || c == 'Z' || c == '-') {
			return false
		}
	}
	return true
}

func randHex(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}
