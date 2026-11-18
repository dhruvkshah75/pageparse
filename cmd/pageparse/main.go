package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	tea "charm.land/bubbletea/v2"
)

const usage = `Usage: run-pageparse [--image | --chat | --both] [--no-start]

  --image     Open Parse and start Nemotron (default)
  --chat      Open Chat and start Ollama
  --both      Open Parse and start both services
  --no-start  Connect to existing services without managing them
`

type request map[string]any

type connection struct {
	State, Endpoint, Message string
	Models                   []string
}

type failure struct {
	Page    *int `json:"page_number"`
	Message string
}

type exportFile struct {
	Format, Status, Staging string
	Path                    string `json:"absolute_path"`
	StageState              string `json:"stage_state"`
	Size                    *int64
}

type exportGroup struct {
	ID, State, Parent string
	Source            string `json:"source_name"`
	Files             []exportFile
}

type backendEvent struct {
	Event, Scope, Name, Model, Destination, State, Endpoint, Message string
	Status, Content, Title                                           string
	Models, Details                                                  []string
	Groups                                                           []exportGroup
	Failure                                                          *failure
	Completed, Total, Width, Height, Revision                        int
	Page                                                             *int `json:"active_page"`
	Elapsed                                                          float64
}

type backendError struct{ message string }

type backend struct {
	child    *exec.Cmd
	requests chan request
	events   chan tea.Msg
}

func startBackend(python, mode string) (*backend, error) {
	cmd := exec.Command(python, "-m", "pageparse.tui_backend", mode)
	// The worker receives EOF on terminal exit and owns the model-server shutdown.
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	input, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}
	output, err := cmd.StdoutPipe()
	if err != nil {
		input.Close()
		return nil, err
	}

	diagnostic := os.Getenv("XDG_STATE_HOME")
	if diagnostic == "" {
		home, _ := os.UserHomeDir()
		diagnostic = filepath.Join(home, ".local", "state")
	}
	diagnostic = filepath.Join(diagnostic, "pageparse", "logs", "tui-backend.log")
	logFile, err := openDiagnostic(diagnostic)
	if err == nil {
		defer logFile.Close()
		cmd.Stderr = logFile
	} else {
		diagnostic = ""
		cmd.Stderr = os.Stderr
	}

	if err := cmd.Start(); err != nil {
		input.Close()
		output.Close()
		return nil, err
	}
	b := &backend{child: cmd, requests: make(chan request, 32), events: make(chan tea.Msg)}
	go b.write(input)
	go b.read(output, diagnostic)
	return b, nil
}

func openDiagnostic(path string) (*os.File, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, err
	}
	return os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
}

func (b *backend) write(input io.WriteCloser) {
	defer input.Close()
	encoder := json.NewEncoder(input)
	for req := range b.requests {
		if err := encoder.Encode(req); err != nil {
			return
		}
	}
}

func (b *backend) read(output io.Reader, diagnostic string) {
	defer close(b.events)
	scanner := bufio.NewScanner(output)
	// The engine retains up to 32 MiB before JSON indentation and escaping.
	scanner.Buffer(make([]byte, 64*1024), 256*1024*1024)
	for scanner.Scan() {
		var event backendEvent
		if err := json.Unmarshal(scanner.Bytes(), &event); err != nil {
			b.events <- backendEvent{Event: "fatal", Message: "The backend returned an invalid message. " + diagnostic}
			continue
		}
		b.events <- event
	}

	message := "The PageParse backend exited."
	if err := scanner.Err(); err != nil {
		message = "Could not read the PageParse backend. " + err.Error()
	}
	if diagnostic != "" {
		message += " See " + diagnostic
	}
	b.events <- backendError{message}
}

func (b *backend) next() tea.Msg { return <-b.events }

func (b *backend) close() error {
	close(b.requests)
	done := make(chan error, 1)
	go func() {
		for range b.events {
		}
		done <- b.child.Wait()
	}()
	select {
	case err := <-done:
		return err
	case <-time.After(35 * time.Second):
		b.child.Process.Kill()
		<-done
		return errors.New("the backend did not exit after its input closed")
	}
}

func parseArgs(args []string) (workspace, mode string, help bool, err error) {
	workspace, mode = "parse", "image"
	chosen, noStart := false, false
	for _, arg := range args {
		switch arg {
		case "-h", "--help":
			return workspace, mode, true, nil
		case "--no-start":
			noStart = true
		case "--image", "--chat", "--both":
			if chosen {
				return "", "", false, errors.New("choose one of --image, --chat, or --both")
			}
			chosen, mode = true, arg[2:]
		default:
			return "", "", false, fmt.Errorf("unknown argument %q", arg)
		}
	}
	if mode == "chat" {
		workspace = "chat"
	}
	if noStart {
		mode = "manual"
	}
	return workspace, mode, false, nil
}

func run(args []string) error {
	workspace, mode, help, err := parseArgs(args)
	if err != nil {
		return err
	}
	if help {
		fmt.Print(usage)
		return nil
	}
	python := os.Getenv("PAGEPARSE_PYTHON")
	if python == "" {
		return errors.New("launch with ./run-pageparse or set PAGEPARSE_PYTHON to the client interpreter")
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGHUP)
	defer stop()
	b, err := startBackend(python, mode)
	if err != nil {
		return err
	}
	program := tea.NewProgram(newModel(b, workspace), tea.WithContext(ctx))
	_, runErr := program.Run()
	closeErr := b.close()
	if ctx.Err() != nil && errors.Is(runErr, tea.ErrProgramKilled) {
		return closeErr
	}
	return errors.Join(runErr, closeErr)
}

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, "PageParse:", err)
		os.Exit(1)
	}
}
