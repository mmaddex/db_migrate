// Command pg-migrate-ui is a local web UI for running several Heroku ->
// Render Postgres migrations, each in its own Render sandbox.
//
// It is a thin layer over the scripts in local/: every button runs one of
// them, with MIGRATION set, and shows its output. The CLI and the UI share
// migrations/<name>/, so either can drive a migration.
//
//	cd webui && go run . -repo ..
//	open http://127.0.0.1:7788
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

func main() {
	addr := flag.String("addr", envOr("PGM_UI_ADDR", "127.0.0.1:7788"), "listen address")
	repo := flag.String("repo", os.Getenv("PGM_REPO"), "repository root (default: found from the working directory)")
	migrations := flag.String("migrations", os.Getenv("MIGRATIONS_DIR"), "migrations directory (default: <repo>/migrations)")
	poll := flag.Duration("poll", 30*time.Second, "how often to refresh each running migration's status")
	allow := flag.String("allow-host", os.Getenv("PGM_UI_ALLOW_HOSTS"), "extra comma-separated Host names to accept (besides localhost)")
	pathPrepend := flag.String("path-prepend", os.Getenv("PGM_UI_PATH_PREPEND"), "directory to put first on PATH for scripts (testing with test/fake-render)")
	flag.Parse()

	root, err := findRepo(*repo)
	if err != nil {
		log.Fatal(err)
	}
	if *migrations == "" {
		*migrations = filepath.Join(root, "migrations")
	}
	if abs, err := filepath.Abs(*migrations); err == nil {
		*migrations = abs
	}
	if err := os.MkdirAll(*migrations, 0o700); err != nil {
		log.Fatal(err)
	}

	password := os.Getenv("PGM_UI_PASSWORD")
	host, _, err := net.SplitHostPort(*addr)
	if err != nil {
		log.Fatalf("bad -addr: %v", err)
	}
	if ip := net.ParseIP(host); (ip == nil || !ip.IsLoopback()) && host != "localhost" && password == "" {
		log.Fatal("refusing to listen on a non-loopback address without PGM_UI_PASSWORD: this UI holds database credentials and runs commands")
	}

	allowed := map[string]bool{"localhost": true, "127.0.0.1": true, "::1": true}
	if ip := net.ParseIP(host); ip != nil && !ip.IsUnspecified() {
		allowed[host] = true
	}
	for _, h := range strings.Split(*allow, ",") {
		if h = strings.TrimSpace(strings.ToLower(h)); h != "" {
			allowed[h] = true
		}
	}

	var extraEnv []string
	if *pathPrepend != "" {
		abs, _ := filepath.Abs(*pathPrepend)
		extraEnv = append(extraEnv, "PATH="+abs+string(os.PathListSeparator)+os.Getenv("PATH"))
	}

	store := &Store{dir: *migrations}
	globalJobs := filepath.Join(*migrations, ".global-jobs")
	s := &Server{
		repoRoot: root,
		store:    store,
		jobs:     NewJobManager(root, *migrations, globalJobs, extraEnv),
		poller:   &Poller{store: store, repoRoot: root, interval: *poll, extraEnv: extraEnv, inflight: map[string]bool{}},
		password: password, allowedHosts: allowed, extraEnv: extraEnv,
		fontsDir: filepath.Join(root, "webui", "fonts"),
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	go s.poller.Run(ctx)

	srv := &http.Server{Addr: *addr, Handler: s.routes(), ReadHeaderTimeout: 10 * time.Second}
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = srv.Shutdown(shutdown)
	}()
	log.Printf("pg-migrate UI on http://%s  (repo %s, migrations %s)", *addr, root, *migrations)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal(err)
	}
}

// findRepo returns the directory containing local/config.sh, starting from
// the flag, then the working directory and its parents.
func findRepo(flagValue string) (string, error) {
	var starts []string
	if flagValue != "" {
		starts = []string{flagValue}
	} else {
		wd, _ := os.Getwd()
		starts = []string{wd}
		if exe, err := os.Executable(); err == nil {
			starts = append(starts, filepath.Dir(exe))
		}
	}
	for _, start := range starts {
		dir, _ := filepath.Abs(start)
		for {
			if _, err := os.Stat(filepath.Join(dir, "local", "config.sh")); err == nil {
				return dir, nil
			}
			if flagValue != "" {
				break
			}
			parent := filepath.Dir(dir)
			if parent == dir {
				break
			}
			dir = parent
		}
	}
	return "", fmt.Errorf("could not find the repository (a directory with local/config.sh); pass -repo")
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
