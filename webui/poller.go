package main

import (
	"context"
	"encoding/json"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"time"
)

// Poller refreshes each migration's cached status by running
// `local/status.sh --json` (which execs status.sh in the sandbox). The UI
// reads the cache, so browsers never trigger sandbox calls directly.
type Poller struct {
	store    *Store
	repoRoot string
	interval time.Duration
	extraEnv []string

	mu       sync.Mutex
	inflight map[string]bool
}

// CachedStatus is what status.json holds.
type CachedStatus struct {
	At     time.Time       `json:"at"`
	Report json.RawMessage `json:"report,omitempty"` // {"sandbox":..., "status":..., "error":...}
	Error  string          `json:"error,omitempty"`
}

func (p *Poller) Run(ctx context.Context) {
	t := time.NewTicker(p.interval)
	defer t.Stop()
	p.pollAll()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			p.pollAll()
		}
	}
}

func (p *Poller) pollAll() {
	ids, err := p.store.List()
	if err != nil {
		log.Printf("poller: %v", err)
		return
	}
	for _, id := range ids {
		if p.store.FileExists(id, ".sandbox_id") {
			go p.Refresh(id)
		}
	}
}

// Refresh polls one migration now. Concurrent calls for the same migration
// collapse into one.
func (p *Poller) Refresh(id string) {
	p.mu.Lock()
	if p.inflight[id] {
		p.mu.Unlock()
		return
	}
	p.inflight[id] = true
	p.mu.Unlock()
	defer func() {
		p.mu.Lock()
		delete(p.inflight, id)
		p.mu.Unlock()
	}()

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
	defer cancel()
	cmd := exec.CommandContext(ctx, "bash", filepath.Join(p.repoRoot, "local", "status.sh"), "--json")
	cmd.Dir = p.repoRoot
	cmd.Env = append(os.Environ(), p.extraEnv...)
	cmd.Env = append(cmd.Env, "MIGRATION="+id, "MIGRATIONS_DIR="+p.store.dir)
	out, err := cmd.Output()

	cs := CachedStatus{At: time.Now().UTC()}
	if err != nil {
		cs.Error = "status check failed: " + err.Error()
		if ee, ok := err.(*exec.ExitError); ok && len(ee.Stderr) > 0 {
			cs.Error = Redact(lastLine(string(ee.Stderr)), p.store.Secrets(id))
		}
		// Keep the previous report so the page still shows the last known state.
		if prev, ok := p.Cached(id); ok {
			cs.Report = prev.Report
		}
	} else if json.Valid(out) {
		cs.Report = json.RawMessage(Redact(string(out), p.store.Secrets(id)))
	} else {
		cs.Error = "status.sh returned invalid JSON"
	}
	b, _ := json.Marshal(cs)
	if err := writeFileAtomic(p.store.path(id, "status.json"), b, 0o644); err != nil {
		log.Printf("poller: write status for %s: %v", id, err)
	}
}

func (p *Poller) Cached(id string) (CachedStatus, bool) {
	var cs CachedStatus
	b, err := os.ReadFile(p.store.path(id, "status.json"))
	if err != nil || json.Unmarshal(b, &cs) != nil {
		return cs, false
	}
	return cs, true
}

func lastLine(s string) string {
	for len(s) > 0 && (s[len(s)-1] == '\n' || s[len(s)-1] == '\r') {
		s = s[:len(s)-1]
	}
	for i := len(s) - 1; i >= 0; i-- {
		if s[i] == '\n' {
			return s[i+1:]
		}
	}
	return s
}
