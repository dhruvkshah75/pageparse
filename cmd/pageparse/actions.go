package main

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"time"
	"unicode"

	tea "charm.land/bubbletea/v2"
	"github.com/charmbracelet/x/ansi"
)

func safeText(value string) string {
	return strings.Map(func(r rune) rune {
		switch r {
		case '\n', '\t':
			return r
		case '\r':
			return -1
		}
		if unicode.IsControl(r) {
			return '�'
		}
		return r
	}, value)
}

func oneLine(value string) string {
	return strings.NewReplacer("\n", " ", "\t", " ").Replace(safeText(value))
}

func choiceLabel(options []choice, value string) string {
	for _, option := range options {
		if option.value == value {
			return option.label
		}
	}
	return value
}

func (m *model) openMenu(kind control, title string, values []choice, selected string) {
	cursor := slices.IndexFunc(values, func(item choice) bool { return item.value == selected })
	m.dialog = &dialog{title: title, choices: values, cursor: max(0, cursor), kind: kind}
}

func (m *model) openBrowser(path string) {
	abs, err := filepath.Abs(path)
	if err != nil {
		m.feedback["parse"] = err.Error()
		return
	}
	entries, err := os.ReadDir(abs)
	if err != nil {
		if m.dialog != nil && m.dialog.kind == browse {
			m.dialog.subtitle = err.Error()
			return
		}
		cwd, cwdErr := os.Getwd()
		if cwdErr == nil && cwd != abs {
			m.openBrowser(cwd)
			return
		}
		m.feedback["parse"] = err.Error()
		return
	}

	options := []choice{}
	if parent := filepath.Dir(abs); parent != abs {
		options = append(options, choice{"../", parent})
	}
	var files []choice
	for _, entry := range entries {
		name := entry.Name()
		if strings.HasPrefix(name, ".") {
			continue
		}
		path := filepath.Join(abs, name)
		isDir := entry.IsDir()
		if !isDir && entry.Type()&os.ModeSymlink != 0 {
			if info, err := os.Stat(path); err == nil {
				isDir = info.IsDir()
			}
		}
		if isDir {
			options = append(options, choice{name + "/", path})
		} else if slices.Contains([]string{".pdf", ".png", ".jpeg", ".jpg", ".webp"}, strings.ToLower(filepath.Ext(name))) {
			files = append(files, choice{name, path})
		}
	}
	m.dialog = &dialog{kind: browse, directory: abs, title: "Browse documents", subtitle: abs,
		choices: append(options, files...)}
}

func (m *model) closeDialog() {
	if m.dialog != nil && m.dialog.kind == helpScreen && m.dialog.underlying != nil {
		m.dialog = m.dialog.underlying
		return
	}
	m.dialog, m.pendingExport = m.pendingExport, nil
}

func (m *model) dismissDialog() {
	if m.dialog.kind == export {
		m.submit(request{"op": "export_cancel"})
	}
	m.closeDialog()
}

func (m *model) dialogKey(key string) tea.Cmd {
	d := m.dialog
	if d.action != "" {
		switch key {
		case "enter":
			if !d.confirmFocused {
				m.dismissDialog()
				return nil
			}
			switch d.kind {
			case export:
				m.submit(request{"op": "export_accept"})
			case deleteFiles:
				m.deleting = true
				m.feedback["exports"] = "Deleting selected files…"
				m.submit(request{"op": "delete", "targets": d.targets})
			}
			m.closeDialog()
		case "esc":
			m.dismissDialog()
		case "tab", "shift+tab":
			d.confirmFocused = !d.confirmFocused
		case "left", "right":
			d.confirmFocused = key == "right"
		case "up", "down", "pgup", "pgdown", "home", "end":
			d.offset = moveIndex(d.offset, key, m.dialogDetailMaxOffset(d), m.dialogRows())
		}
		return nil
	}
	switch key {
	case "esc":
		m.closeDialog()
	case "backspace", "left":
		if d.kind == browse {
			m.openBrowser(filepath.Dir(d.directory))
		}
	case "~":
		if d.kind == browse {
			if home, err := os.UserHomeDir(); err == nil {
				m.openBrowser(home)
			}
		}
	case "enter", "right", "space", " ":
		if len(d.choices) > 0 && (key != "right" || d.kind == browse) {
			m.choose(d, d.choices[d.cursor])
		}
	default:
		if len(d.choices) > 0 {
			d.cursor = moveIndex(d.cursor, key, len(d.choices)-1, m.dialogRows())
		}
	}
	return nil
}

func moveIndex(current int, key string, last, pageSize int) int {
	switch key {
	case "up":
		current--
	case "down":
		current++
	case "pgup":
		current -= pageSize
	case "pgdown":
		current += pageSize
	case "home":
		current = 0
	case "end":
		current = last
	}
	return min(max(0, current), max(0, last))
}

func (m *model) choose(d *dialog, item choice) {
	if d.kind == browse {
		if info, err := os.Stat(item.value); err == nil && info.IsDir() {
			m.openBrowser(item.value)
			return
		}
		m.inputs[source].SetValue(item.value)
		m.invalidateClipboard()
		m.closeDialog()
		return
	}
	if d.kind == deleteFiles {
		m.dialog = nil
		m.confirmDeletion(item.value)
		if m.dialog == nil {
			m.closeDialog()
		}
		return
	}
	m.closeDialog()
	switch d.kind {
	case format:
		m.format = item.value
	case passMode:
		m.passMode = item.value
	case previewMode:
		if item.value != m.previewMode {
			m.previewMode, m.previewRaw = item.value, ""
			m.updatePreview()
			m.preview.GotoTop()
			m.requestPreview()
		}
	case chatModel:
		m.useChatModel(item.value)
	}
}

func (m *model) confirmDeletion(format string) {
	var targets [][2]string
	var paths []string
	var known int64
	unknown := 0
	for _, group := range m.groups {
		if !m.selection[group.ID] {
			continue
		}
		for _, file := range group.Files {
			if format == "both" || format == file.Format {
				targets = append(targets, [2]string{group.ID, file.Format})
				paths = append(paths, file.Path)
				if file.Size == nil {
					unknown++
				} else {
					known += *file.Size
				}
			}
		}
	}
	if len(targets) == 0 {
		m.feedback["exports"] = "The selected groups have no files in that format."
		return
	}
	summary := fmt.Sprintf("%d known bytes", known)
	if unknown > 0 {
		summary += fmt.Sprintf(" · %d unknown sizes", unknown)
	}
	m.dialog = &dialog{kind: deleteFiles, title: "Permanently delete these files?",
		action: "Delete", detail: strings.Join(paths, "\n") + "\n\n" + summary, targets: targets}
}

func (m *model) statusText() string {
	if m.offline != "" {
		return m.offline
	}
	return m.feedback[m.workspace]
}

func (m *model) openStatus() {
	if text := m.statusText(); text != "" {
		m.dialog = &dialog{title: "Status", detail: text, action: "Close"}
	}
}

func (m *model) setGroups(groups []exportGroup) {
	previous := ""
	if len(m.groups) > 0 {
		previous = m.groups[m.groupIndex].ID
	}
	for id := range m.selection {
		if !slices.ContainsFunc(groups, func(group exportGroup) bool { return group.ID == id }) {
			delete(m.selection, id)
		}
	}
	m.groups = groups
	if index := slices.IndexFunc(groups, func(group exportGroup) bool { return group.ID == previous }); index >= 0 {
		m.groupIndex = index
	} else {
		m.groupIndex = min(m.groupIndex, max(0, len(groups)-1))
	}
	m.updateExportDetails(previous == "" || len(groups) == 0 || groups[m.groupIndex].ID != previous)
}

func (m *model) moveGroup(key string) {
	next := moveIndex(m.groupIndex, key, len(m.groups)-1, m.groupRows())
	if next != m.groupIndex {
		m.groupIndex = next
		m.updateExportDetails(true)
	}
}

func (m *model) groupText() string {
	if len(m.groups) == 0 {
		return ""
	}
	group := m.groups[m.groupIndex]
	details := []string{oneLine(group.Source) + " · " + oneLine(group.State)}
	for _, file := range group.Files {
		meta := strings.ToUpper(file.Format) + " · " + oneLine(file.Status)
		if file.Size != nil {
			meta += fmt.Sprintf(" · %d B", *file.Size)
		}
		details = append(details, "", meta, oneLine(file.Path))
		if file.StageState != "resolved" {
			details = append(details, "Staging · "+oneLine(file.StageState), oneLine(filepath.Join(group.Parent, file.Staging)))
		}
	}
	return strings.Join(details, "\n")
}

func (m *model) copy(focusedInput bool) tea.Cmd {
	var content string
	if m.dialog != nil {
		content = safeText(m.dialog.detail)
	} else {
		switch m.workspace {
		case "parse":
			if input := m.inputs[m.focus]; focusedInput && input != nil && input.Value() != "" {
				content = input.Value()
			} else {
				content = m.previewRaw
			}
		case "chat":
			if focusedInput && m.focus == prompt && m.prompt.Value() != "" {
				content = m.prompt.Value()
			} else if len(m.turns) > 0 {
				content = ansi.Strip(m.transcriptText(false))
			}
		case "exports":
			content = m.groupText()
		}
	}
	if strings.TrimSpace(content) == "" {
		m.feedback[m.workspace] = "There is no text to copy here."
		return nil
	}
	workspace := m.workspace
	return func() tea.Msg {
		result := clipboardResult{workspace: workspace}
		if !writeDesktopClipboard(content) {
			result.fallback = content
		}
		return result
	}
}

type clipboardResult struct{ workspace, fallback string }

func writeDesktopClipboard(text string) bool {
	var commands [][]string
	if os.Getenv("WAYLAND_DISPLAY") != "" {
		commands = append(commands, []string{"wl-copy", "--type", "text/plain;charset=utf-8"})
	}
	if os.Getenv("DISPLAY") != "" {
		commands = append(commands, []string{"xclip", "-selection", "clipboard", "-in"})
	}
	for _, args := range commands {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		cmd := exec.CommandContext(ctx, args[0], args[1:]...)
		cmd.Stdin = strings.NewReader(text)
		err := cmd.Run()
		cancel()
		if err == nil {
			return true
		}
	}
	return false
}
