package main

import (
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"time"

	"charm.land/bubbles/v2/textarea"
	"charm.land/bubbles/v2/textinput"
	"charm.land/bubbles/v2/viewport"
	tea "charm.land/bubbletea/v2"
)

type control string

const (
	source        control = "source"
	browse        control = "browse"
	pages         control = "pages"
	destination   control = "destination"
	format        control = "format"
	passMode      control = "pass mode"
	parseStart    control = "parse start"
	parseStop     control = "parse stop"
	export        control = "export"
	previewMode   control = "preview mode"
	preview       control = "preview"
	chatModel     control = "model"
	newChat       control = "new chat"
	transcript    control = "transcript"
	prompt        control = "prompt"
	send          control = "send"
	chatStop      control = "chat stop"
	exportList    control = "export list"
	exportDetails control = "export details"
	deleteFiles   control = "delete"
	helpScreen    control = "help"
)

var workspacesByKey = map[string]string{"f1": "parse", "f2": "chat", "f3": "exports"}

var workspaceControls = map[string][]control{
	"parse":   {source, browse, pages, destination, format, passMode, parseStart, parseStop, export, previewMode, preview},
	"chat":    {chatModel, newChat, transcript, prompt, send, chatStop},
	"exports": {exportList, exportDetails, deleteFiles},
}

type choice struct{ label, value string }

var outputFormats = []choice{{"Markdown + JSON", "both"}, {"Markdown", "md"}, {"JSON", "json"}}
var previewModes = []choice{{"Rendered", "rendered"}, {"Markdown", "markdown"}, {"JSON", "json"}}
var passModes = []choice{{"Single · fast preview", "single"}, {"Double · refine tables and charts", "double"},
	{"Triple · detect and refine tables and charts", "triple"}}

type dialog struct {
	title, subtitle           string
	choices                   []choice
	cursor, offset            int
	detail, action, directory string
	targets                   [][2]string
	confirmFocused            bool
	kind                      control
	underlying                *dialog
}

type turn struct{ prompt, reply, state string }

type heartbeat time.Time

type pastedSourceText struct {
	revision int
	message  tea.Msg
}

type model struct {
	backend                      *backend
	width, height                int
	workspace                    string
	focus                        control
	inputs                       map[control]*textinput.Model
	prompt                       textarea.Model
	preview, transcript, details viewport.Model
	dialog                       *dialog
	pendingExport                *dialog

	connections                               map[string]connection
	modelID, chatID                           string
	slot, offline                             string
	parseStatus                               string
	feedback                                  map[string]string
	jobID, completed, total                   int
	page                                      *int
	elapsed                                   float64
	stopping, exporting, deleting, indexReady bool
	clipboard, pasting                        bool
	sourceRevision                            int
	format, previewMode, passMode             string
	previewRaw                                string
	previewRevision, previewPending           int
	renderWidth                               int
	renderRaw                                 string
	renderBusy                                bool
	turns                                     []turn
	retained                                  int

	groups     []exportGroup
	selection  map[string]bool
	groupIndex int
}

func newModel(b *backend, workspace string) *model {
	m := &model{
		backend: b, width: 80, height: 24, workspace: workspace,
		inputs: make(map[control]*textinput.Model), feedback: make(map[string]string),
		connections: make(map[string]connection), selection: make(map[string]bool),
		format: "both", previewMode: "rendered", passMode: "single",
		preview: viewport.New(), transcript: viewport.New(), details: viewport.New(),
		prompt: textarea.New(),
	}
	m.preview.SoftWrap, m.transcript.SoftWrap, m.details.SoftWrap = true, true, true
	for field, hint := range map[control]string{source: "Path to PDF or image", pages: "all or 1,3-5", destination: "outputs/"} {
		input := textinput.New()
		input.Prompt, input.Placeholder = "", hint
		input.CharLimit = 4096
		styles := textinput.DefaultDarkStyles()
		styles.Focused.Text = lipText
		styles.Blurred.Text = lipText
		styles.Focused.Placeholder = lipMuted
		styles.Blurred.Placeholder = lipMuted
		styles.Cursor.Color = coral
		styles.Cursor.Blink = false
		input.SetStyles(styles)
		m.inputs[field] = &input
	}
	m.prompt.Prompt, m.prompt.Placeholder = "", "Write a message"
	m.prompt.ShowLineNumbers = false
	m.prompt.CharLimit = 64 * 1024
	m.prompt.MaxHeight = 4
	styles := textarea.DefaultDarkStyles()
	styles.Focused.Base, styles.Blurred.Base = lipText, lipText
	styles.Focused.Text, styles.Blurred.Text = lipText, lipText
	styles.Focused.CursorLine, styles.Blurred.CursorLine = lipText, lipText
	styles.Focused.Placeholder, styles.Blurred.Placeholder = lipMuted, lipMuted
	styles.Cursor.Color = coral
	styles.Cursor.Blink = false
	m.prompt.SetStyles(styles)
	m.updateTranscript(false)
	m.updatePreview()
	m.updateExportDetails(false)
	m.setFocus(defaultFocus(workspace))
	return m
}

func defaultFocus(workspace string) control {
	switch workspace {
	case "chat":
		return prompt
	case "exports":
		return exportList
	default:
		return source
	}
}

func (m *model) setFocus(next control) tea.Cmd {
	for _, input := range m.inputs {
		input.Blur()
	}
	m.prompt.Blur()
	m.focus = next
	m.resize()
	if input := m.inputs[next]; input != nil {
		return input.Focus()
	}
	if next == prompt {
		return m.prompt.Focus()
	}
	return nil
}

func tick() tea.Cmd {
	return tea.Tick(2*time.Second, func(t time.Time) tea.Msg { return heartbeat(t) })
}

func (m *model) Init() tea.Cmd {
	if m.backend == nil {
		return nil
	}
	return tea.Batch(m.backend.next, tick(), m.setFocus(m.focus))
}

func (m *model) submit(req request) {
	if m.backend != nil && m.offline == "" {
		select {
		case m.backend.requests <- req:
		default:
			m.offline = "The backend command queue is full. Restart PageParse to reconnect."
		}
	}
}

func (m *model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	cmd := m.update(msg)
	return m, tea.Batch(cmd, m.queueRender())
}

func (m *model) update(msg tea.Msg) tea.Cmd {
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.width, m.height = msg.Width, msg.Height
		m.resize()
	case markdownRendered:
		m.renderBusy = false
		if m.previewMode == "rendered" && msg.revision == m.previewRevision && msg.width == m.preview.Width() && msg.text != "" {
			m.preview.SetContent(msg.text)
			m.resize()
		} else if msg.revision != m.previewRevision {
			m.renderRaw = ""
		}
	case pastedSourceText:
		if m.workspace == "parse" && m.focus == source && msg.revision == m.sourceRevision {
			return m.updateWidget(msg.message)
		}
	case backendEvent:
		m.receive(msg)
		if msg.Event == "paste_text" && m.backend != nil {
			revision := msg.Revision
			return tea.Batch(m.backend.next, func() tea.Msg {
				return pastedSourceText{revision, textinput.Paste()}
			})
		}
		if m.backend != nil {
			return m.backend.next
		}
	case backendError:
		if m.offline == "" {
			m.offline = msg.message
		}
	case heartbeat:
		if m.workspace == "exports" && m.dialog == nil && !m.deleting {
			m.submit(request{"op": "index"})
		}
		return tick()
	case clipboardResult:
		if msg.fallback != "" {
			m.feedback[msg.workspace] = "Sent to the terminal clipboard (OSC 52)."
			return tea.SetClipboard(msg.fallback)
		}
		m.feedback[msg.workspace] = "Copied to clipboard."
	case pointerAction:
		return m.usePointer(msg)
	case tea.KeyPressMsg:
		return m.handleKey(msg)
	default:
		if m.dialog == nil {
			return m.updateWidget(msg)
		}
	}
	return nil
}

func (m *model) receive(event backendEvent) {
	switch event.Event {
	case "session":
		m.modelID = event.Model
		if m.inputs[destination].Value() == "" {
			m.inputs[destination].SetValue(event.Destination)
			m.inputs[destination].CursorStart()
		}
	case "connection":
		m.connections[event.Name] = connection{event.State, event.Endpoint, event.Message, event.Models}
		if event.Name == "Ollama" && m.slot != "chat" {
			m.syncChatModel()
		}
	case "paste_text":
		m.pasting = false
	case "clipboard":
		m.pasting = false
		if m.sourceRevision == event.Revision {
			m.clipboard = true
			m.inputs[source].SetValue("")
			m.inputs[source].Placeholder = fmt.Sprintf("Clipboard image · %d×%d", event.Width, event.Height)
			m.inputs[pages].SetValue("")
			m.feedback["parse"] = "Clipboard image ready."
		}
	case "progress":
		if m.completed != event.Completed && event.State == "running" {
			m.completed = event.Completed
			m.requestPreview()
		}
		m.parseStatus, m.completed, m.total = event.State, event.Completed, event.Total
		m.page, m.elapsed = event.Page, event.Elapsed
	case "parse_done":
		m.slot, m.stopping = "", false
		m.parseStatus, m.completed = event.Status, event.Completed
		if event.Failure != nil {
			m.feedback["parse"] = event.Failure.Message
			if event.Failure.Page != nil {
				m.feedback["parse"] = fmt.Sprintf("Page %d · %s", *event.Failure.Page, event.Failure.Message)
			}
		} else if event.Status == "cancelled" {
			m.feedback["parse"] = "Cancelled. Completed pages are available."
		}
		m.requestPreview()
	case "preview":
		m.previewPending = 0
		if event.Revision == m.previewRevision {
			if m.previewRaw != event.Content {
				m.previewRaw = event.Content
				m.updatePreview()
			}
		} else if m.completed > 0 {
			m.sendPreview()
		}
	case "chunk":
		if len(m.turns) > 0 {
			last := &m.turns[len(m.turns)-1]
			last.reply += event.Content
			m.retained += len(event.Content)
			m.updateTranscript(m.transcript.AtBottom())
		}
	case "chat_done":
		m.finishChat(event.Status, event.Message)
	case "index":
		m.setGroups(event.Groups)
		m.indexReady = true
	case "export_confirm":
		// An asynchronous proposal can arrive while another chooser is open.
		// Keep the export pending and show the exact paths when that chooser closes.
		m.pendingExport = &dialog{title: event.Title, detail: strings.Join(event.Details, "\n"), action: "Export", kind: export}
		if m.dialog == nil {
			m.closeDialog()
		}
	case "export_done":
		m.exporting = false
		m.feedback["parse"] = event.Message
		m.submit(request{"op": "index"})
	case "delete_done":
		m.deleting = false
		m.feedback["exports"] = event.Message
		clear(m.selection)
		m.submit(request{"op": "index"})
	case "error":
		m.handleBackendError(event)
	case "fatal":
		m.offline = event.Message
	}
}

func (m *model) handleBackendError(event backendEvent) {
	workspace := "parse"
	switch event.Scope {
	case "Nemotron", "Ollama":
		m.connections[event.Scope] = connection{State: "failed", Message: event.Message}
		return
	case "parse":
		m.slot, m.stopping, m.parseStatus = "", false, "failed"
	case "chat":
		m.finishChat("failed", event.Message)
		return
	case "preview":
		m.previewPending = 0
	case "paste":
		m.pasting = false
	case "export":
		m.exporting = false
	case "index":
		m.indexReady = false
		workspace = "exports"
	case "delete":
		m.deleting = false
		workspace = "exports"
	}
	m.feedback[workspace] = event.Message
}

func (m *model) requestPreview() {
	m.previewRevision++
	if m.previewPending == 0 && m.completed > 0 {
		m.sendPreview()
	}
}

func (m *model) sendPreview() {
	m.previewPending = m.previewRevision
	m.submit(request{"op": "preview", "mode": m.previewMode, "revision": m.previewPending})
}

func (m *model) updateWidget(msg tea.Msg) tea.Cmd {
	var cmd tea.Cmd
	if input := m.inputs[m.focus]; input != nil {
		before := input.Value()
		*input, cmd = input.Update(msg)
		if m.focus == source && input.Value() != before {
			m.invalidateClipboard()
		}
	} else {
		switch m.focus {
		case prompt:
			m.prompt, cmd = m.prompt.Update(msg)
		case preview:
			m.preview, cmd = m.preview.Update(msg)
		case transcript:
			m.transcript, cmd = m.transcript.Update(msg)
		case exportDetails:
			m.details, cmd = m.details.Update(msg)
		}
	}
	return cmd
}

func (m *model) invalidateClipboard() {
	m.sourceRevision++
	m.clipboard = false
	m.inputs[source].Placeholder = "Path to PDF or image"
}

func (m *model) handleKey(key tea.KeyPressMsg) tea.Cmd {
	name := key.String()
	if name == "ctrl+q" {
		return tea.Quit
	}
	if name == "ctrl+c" || name == "f9" {
		return m.copy(name == "ctrl+c")
	}
	stroke := key.Keystroke()
	switch stroke {
	case "alt+?", "alt+shift+?", "alt+shift+/":
		if m.dialog == nil {
			return m.updateWidget(tea.KeyPressMsg{Code: '?', Text: "?"})
		}
		return nil
	}
	if name == "f11" || name == "?" || stroke == "shift+/" || stroke == "shift+?" {
		m.toggleHelp()
		return nil
	}
	if m.dialog != nil {
		return m.dialogKey(name)
	}
	switch name {
	case "f1", "f2", "f3":
		return m.switchWorkspace(workspacesByKey[name])
	case "f4":
		m.submit(request{"op": "refresh"})
	case "f5":
		switch m.workspace {
		case "parse":
			m.startParse()
		case "chat":
			m.startChat()
		}
	case "f6":
		m.stop()
	case "f7":
		if m.workspace == "parse" {
			m.startExport()
		}
	case "f10":
		m.openStatus()
	case "f8", "ctrl+v":
		if name == "ctrl+v" && (m.workspace != "parse" || m.focus == pages || m.focus == destination) {
			return m.updateWidget(key)
		}
		if m.workspace == "parse" && !m.pasting && m.offline == "" {
			m.pasting = true
			m.submit(request{"op": "paste", "revision": m.sourceRevision,
				"text_fallback": name == "ctrl+v" && m.focus == source})
		}
	case "tab", "shift+tab":
		controls := workspaceControls[m.workspace]
		move := 1
		if name == "shift+tab" {
			move = -1
		}
		index := (slices.Index(controls, m.focus) + move + len(controls)) % len(controls)
		return m.setFocus(controls[index])
	case "enter", "space", " ":
		if m.focus == exportList || m.inputs[m.focus] == nil && m.focus != prompt &&
			m.focus != preview && m.focus != transcript && m.focus != exportDetails {
			return m.activate()
		}
		return m.updateWidget(key)
	default:
		if m.workspace == "parse" {
			if next := parseArrowFocus(m.focus, name); next != "" {
				return m.setFocus(next)
			}
		}
		if m.focus == exportList {
			m.moveGroup(name)
			return nil
		}
		if name == "up" || name == "down" {
			switch m.focus {
			case previewMode, chatModel, deleteFiles:
				m.activate()
				if m.dialog != nil {
					m.dialogKey(name)
				}
				return nil
			}
		}
		return m.updateWidget(key)
	}
	return nil
}

func (m *model) switchWorkspace(workspace string) tea.Cmd {
	m.workspace = workspace
	if workspace == "exports" {
		m.submit(request{"op": "index"})
	}
	return m.setFocus(defaultFocus(workspace))
}

func parseArrowFocus(current control, key string) control {
	controls := workspaceControls["parse"]
	index := slices.Index(controls, current)
	switch current {
	case source, browse, pages, destination, format, passMode:
		if key == "down" {
			return controls[index+1]
		}
		if key == "up" && current != source {
			return controls[index-1]
		}
	case parseStart, parseStop, export:
		switch key {
		case "up":
			return passMode
		case "left":
			if current != parseStart {
				return controls[index-1]
			}
		case "right":
			if current != export {
				return controls[index+1]
			}
		}
	}
	return ""
}

func (m *model) enabled(field control) bool {
	if m.offline != "" {
		switch field {
		case parseStart, parseStop, export, send, chatStop, deleteFiles:
			return false
		}
	}
	switch field {
	case parseStart:
		return m.slot == "" && !m.pasting && m.connections["Nemotron"].State == "ready" &&
			(m.clipboard || strings.TrimSpace(m.inputs[source].Value()) != "")
	case parseStop:
		return m.slot == "parse" && !m.stopping
	case export:
		return m.slot != "parse" && m.completed > 0 && m.indexReady && !m.exporting
	case chatModel:
		return m.slot != "chat" && len(m.connections["Ollama"].Models) > 0
	case newChat:
		return m.slot != "chat" && len(m.turns) > 0
	case send:
		conn := m.connections["Ollama"]
		return m.slot == "" && conn.State == "ready" && slices.Contains(conn.Models, m.chatID) &&
			strings.TrimSpace(m.prompt.Value()) != ""
	case chatStop:
		return m.slot == "chat" && !m.stopping
	case deleteFiles:
		return !m.deleting && m.indexReady && len(m.selection) > 0
	default:
		return true
	}
}

func (m *model) activate() tea.Cmd {
	if !m.enabled(m.focus) {
		return nil
	}
	switch m.focus {
	case browse:
		m.openBrowser(m.browseStart())
	case format:
		m.openMenu(format, "Export format", outputFormats, m.format)
	case passMode:
		m.openMenu(passMode, "Nemotron pass mode", passModes, m.passMode)
	case previewMode:
		m.openMenu(previewMode, "Preview mode", previewModes, m.previewMode)
	case chatModel:
		var options []choice
		for _, modelID := range m.connections["Ollama"].Models {
			options = append(options, choice{modelID, modelID})
		}
		m.openMenu(chatModel, "Ollama model", options, m.chatID)
	case parseStart:
		m.startParse()
	case parseStop, chatStop:
		m.stop()
	case export:
		m.startExport()
	case newChat:
		m.resetChat()
	case send:
		m.startChat()
	case exportList:
		if len(m.groups) > 0 {
			id := m.groups[m.groupIndex].ID
			if m.selection[id] {
				delete(m.selection, id)
			} else {
				m.selection[id] = true
			}
		}
	case deleteFiles:
		m.openMenu(deleteFiles, "Choose files to delete", outputFormats, "both")
	}
	return nil
}

func (m *model) startParse() {
	if !m.enabled(parseStart) {
		if m.offline == "" && m.slot == "" {
			m.feedback["parse"] = "Select a source and wait for Nemotron to be ready."
		}
		return
	}
	m.jobID++
	m.slot, m.parseStatus = "parse", "validating"
	m.completed, m.total, m.elapsed, m.page = 0, 0, 0, nil
	m.previewRevision++
	m.previewRaw = ""
	m.updatePreview()
	m.preview.GotoTop()
	m.feedback["parse"] = ""
	m.submit(request{"op": "parse", "id": m.jobID, "clipboard": m.clipboard,
		"source": m.inputs[source].Value(), "pages": m.inputs[pages].Value(), "pass_mode": m.passMode})
}

func (m *model) stop() {
	if m.workspace == "parse" && m.enabled(parseStop) {
		m.stopping = true
		m.submit(request{"op": "stop_parse"})
	} else if m.workspace == "chat" && m.enabled(chatStop) {
		m.stopping = true
		m.submit(request{"op": "stop_chat"})
	}
}

func (m *model) startExport() {
	if m.enabled(export) {
		m.exporting = true
		m.feedback["parse"] = "Preparing export…"
		m.submit(request{"op": "export", "format": m.format, "destination": m.inputs[destination].Value()})
	}
}

func (m *model) startChat() {
	if !m.enabled(send) {
		if m.offline == "" && m.slot == "" {
			m.feedback["chat"] = "Write a message and select a ready Ollama model."
		}
		return
	}
	value := strings.TrimSpace(m.prompt.Value())
	if m.retained+len(value) > 256*1024 {
		m.feedback["chat"] = "Transcript limit reached. Start a new chat or shorten the message."
		return
	}
	type message struct {
		Role    string `json:"role"`
		Content string `json:"content"`
	}
	history := make([]message, 0, len(m.turns)*2+1)
	for _, past := range m.turns {
		if past.state == "complete" {
			history = append(history, message{"user", past.prompt}, message{"assistant", past.reply})
		}
	}
	history = append(history, message{"user", value})
	m.retained += len(value)
	m.turns = append(m.turns, turn{prompt: value, state: "streaming"})
	m.slot = "chat"
	m.prompt.Reset()
	m.feedback["chat"] = "Streaming…"
	m.updateTranscript(true)
	m.submit(request{"op": "chat", "model": m.chatID, "retained": m.retained, "messages": history})
}

func (m *model) finishChat(state, message string) {
	if len(m.turns) > 0 {
		m.turns[len(m.turns)-1].state = state
	}
	m.slot, m.stopping = "", false
	m.feedback["chat"] = message
	m.updateTranscript(m.transcript.AtBottom())
	m.syncChatModel()
}

func (m *model) syncChatModel() {
	conn := m.connections["Ollama"]
	if conn.State == "offline" || slices.Contains(conn.Models, m.chatID) {
		return
	}
	next := ""
	if slices.Contains(conn.Models, "qwen2.5-coder:3b") {
		next = "qwen2.5-coder:3b"
	} else if len(conn.Models) > 0 {
		next = conn.Models[0]
	}
	m.useChatModel(next)
}

func (m *model) useChatModel(id string) {
	if id != m.chatID {
		m.chatID = id
		m.resetChat()
	}
}

func (m *model) resetChat() {
	m.turns, m.retained = nil, 0
	m.feedback["chat"] = ""
	m.updateTranscript(false)
	m.transcript.GotoTop()
}

func (m *model) browseStart() string {
	value := strings.Trim(strings.TrimSpace(m.inputs[source].Value()), "\"'")
	if strings.HasPrefix(value, "~/") {
		if home, err := os.UserHomeDir(); err == nil {
			value = filepath.Join(home, value[2:])
		}
	}
	if info, err := os.Stat(value); err == nil && info.IsDir() {
		return value
	}
	return filepath.Dir(value)
}
