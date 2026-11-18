package main

import (
	"fmt"
	"strings"
	"testing"

	tea "charm.land/bubbletea/v2"
	"github.com/charmbracelet/x/ansi"
)

func press(m *model, code rune) {
	m.Update(tea.KeyPressMsg{Code: code})
}

func TestResponsiveWorkspaces(t *testing.T) {
	for _, size := range [][2]int{{30, 10}, {44, 15}, {68, 24}, {80, 18}, {122, 32}} {
		for workspace, controls := range workspaceControls {
			t.Run(fmt.Sprintf("%s/%dx%d", workspace, size[0], size[1]), func(t *testing.T) {
				m := newModel(nil, workspace)
				m.completed, m.total = 1, 3
				m.chatID = strings.Repeat("測", 50)
				m.setGroups([]exportGroup{{ID: "id", Source: strings.Repeat("文", 60)}})
				m.Update(tea.WindowSizeMsg{Width: size[0], Height: size[1]})

				for _, focus := range controls {
					m.setFocus(focus)
					lines := strings.Split(m.View().Content, "\n")
					if len(lines) > size[1] {
						t.Fatalf("%s: %d lines in a %d-row terminal", focus, len(lines), size[1])
					}
					for _, line := range lines {
						if width := ansi.StringWidth(line); width > size[0] {
							t.Errorf("%s: %d columns in a %d-column terminal: %q", focus, width, size[0], ansi.Strip(line))
						}
					}
				}
			})
		}
	}
}

func TestKeyboardDropdowns(t *testing.T) {
	m := newModel(nil, "parse")
	m.setFocus(format)
	press(m, tea.KeyDown)
	if m.dialog == nil || m.dialog.choices[m.dialog.cursor].value != "md" {
		t.Fatal("Down should open the format menu and move to Markdown")
	}
	press(m, tea.KeyDown)
	press(m, tea.KeyEnter)
	if m.dialog != nil || m.format != "json" || m.focus != format {
		t.Fatal("Enter should apply JSON and restore focus")
	}
	m.setFocus(previewMode)
	press(m, tea.KeyEnter)
	press(m, tea.KeyDown)
	press(m, tea.KeyEsc)
	if m.previewMode != "rendered" || m.dialog != nil {
		t.Fatal("Esc should leave the preview mode unchanged")
	}
	press(m, tea.KeyF2)
	m.receive(backendEvent{Event: "connection", Name: "Ollama", State: "ready",
		Models: []string{"alternate", "qwen2.5-coder:3b"}})
	m.setFocus(chatModel)
	press(m, tea.KeyEnter)
	press(m, tea.KeyUp)
	press(m, tea.KeyEnter)
	if m.chatID != "alternate" {
		t.Fatalf("model changed to %q", m.chatID)
	}
}

func TestCopyUsesRawPreviewAndFocusedInput(t *testing.T) {
	t.Setenv("DISPLAY", "")
	t.Setenv("WAYLAND_DISPLAY", "")
	m := newModel(nil, "parse")
	m.inputs[source].SetValue("/tmp/my file.pdf")
	m.previewRaw = "### Page 1\n\nReport · 進捗\n"
	cases := []struct {
		focusedInput bool
		want         string
	}{
		{false, m.previewRaw},
		{true, "/tmp/my file.pdf"},
	}
	for _, tc := range cases {
		command := m.copy(tc.focusedInput)
		if command == nil {
			t.Fatal("expected clipboard command")
		}
		result := command().(clipboardResult)
		if result.fallback != tc.want {
			t.Errorf("clipboard payload = %q, want %q", result.fallback, tc.want)
		}
	}
}

func TestRenderedPreviewDisplaysHeadingsAndTable(t *testing.T) {
	m := newModel(nil, "parse")
	m.previewRaw = "### Page 1\n\n| Item | Value |\n| --- | --- |\n| A | 9 |\n"
	rendered := m.queueRender()().(markdownRendered)
	plain := ansi.Strip(rendered.text)
	if !strings.Contains(plain, "Page 1") || strings.Contains(plain, "###") || !strings.Contains(plain, "Item") {
		t.Fatalf("unexpected rendered preview: %q", plain)
	}
	m.Update(rendered)
	if !strings.Contains(ansi.Strip(m.View().Content), "Page 1") {
		t.Fatal("rendered preview is not visible")
	}
}

func TestLaunchModes(t *testing.T) {
	workspace, mode, _, err := parseArgs([]string{"--chat", "--no-start"})
	if err != nil || workspace != "chat" || mode != "manual" {
		t.Fatalf("unexpected chat arguments: %s %s %v", workspace, mode, err)
	}
	if _, _, _, err := parseArgs([]string{"--image", "--both"}); err == nil {
		t.Fatal("expected conflicting mode flags to fail")
	}
}

func TestPointerOpensAndChoosesDropdown(t *testing.T) {
	m := newModel(nil, "parse")
	m.Update(tea.WindowSizeMsg{Width: 110, Height: 26})
	click := func(x, y int) {
		t.Helper()
		view := m.View()
		cmd := view.OnMouse(tea.MouseClickMsg{X: x, Y: y, Button: tea.MouseLeft})
		if cmd == nil {
			t.Fatalf("nothing at %d,%d", x, y)
		}
		m.Update(cmd())
	}
	click(16, 8)
	if m.dialog == nil || m.dialog.kind != format {
		t.Fatal("format dropdown did not open")
	}
	click(8, 5)
	if m.dialog != nil || m.format != "md" {
		t.Fatalf("click did not select Markdown: %q", m.format)
	}
	click(25, 0)
	if m.workspace != "chat" || m.focus != prompt {
		t.Fatal("click did not open Chat")
	}
}

func TestDarkTerminalPalette(t *testing.T) {
	t.Setenv("TERM", "xterm-256color")
	t.Setenv("NO_COLOR", "")
	view := newModel(nil, "parse").View()
	if view.BackgroundColor == nil || view.ForegroundColor == nil {
		t.Fatal("expected the app to set terminal canvas and text colors")
	}
	r, g, b, _ := view.BackgroundColor.RGBA()
	if r>>8 != 0x1f || g>>8 != 0x1e || b>>8 != 0x1b {
		t.Fatalf("unexpected canvas color: %02x%02x%02x", r>>8, g>>8, b>>8)
	}
}
