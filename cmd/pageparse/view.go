package main

import (
	"fmt"
	"os"
	"strings"

	"charm.land/bubbles/v2/viewport"
	tea "charm.land/bubbletea/v2"
	"charm.land/glamour/v2"
	"charm.land/lipgloss/v2"
	"github.com/charmbracelet/x/ansi"
)

const (
	canvasHex       = "#1f1e1b"
	surfaceHex      = "#252320"
	outputHex       = "#181715"
	textHex         = "#faf9f5"
	mutedHex        = "#a09d96"
	coralHex        = "#cc785c"
	onCoralHex      = "#141413"
	hairlineHex     = "#4b443d"
	amberHex        = "#e8a55a"
	formLabel       = 9
	formIndent      = 2 + formLabel
	parseFormHeight = 11
)

var (
	canvas        = lipgloss.Color(canvasHex)
	coral         = lipgloss.Color(coralHex)
	cream         = lipgloss.Color(textHex)
	surface       = lipgloss.Color(surfaceHex)
	dark          = lipgloss.Color(outputHex)
	lipText       = lipgloss.NewStyle().Foreground(cream)
	lipAction     = lipgloss.NewStyle().Foreground(lipgloss.Color(onCoralHex)).Background(coral).Bold(true)
	lipMuted      = lipgloss.NewStyle().Foreground(lipgloss.Color(mutedHex))
	lipAccent     = lipgloss.NewStyle().Foreground(coral)
	lipLine       = lipgloss.NewStyle().Foreground(lipgloss.Color(hairlineHex))
	lipDark       = lipText.Background(dark)
	lipDarkMuted  = lipMuted.Background(dark)
	lipDarkAccent = lipgloss.NewStyle().Foreground(lipgloss.Color(amberHex)).Background(dark).Bold(true)
)

type layout struct {
	width, height, form, viewer int
	split, short                bool
}

func (m *model) layout() layout {
	width := max(1, m.width-2)
	l := layout{width: width, height: max(1, m.height-5), form: width, viewer: width}
	l.split = m.width >= 80
	l.short = l.height < 15
	if l.split {
		l.form = min(46, width*43/100)
		l.viewer = width - l.form - 2
	}
	return l
}

func (m *model) resize() {
	l := m.layout()
	for _, field := range []control{source, pages, destination} {
		m.inputs[field].SetWidth(max(1, l.form-formIndent-1))
	}
	m.preview.SetWidth(max(1, l.viewer-4))
	previewHeight := l.height
	if !l.split && !l.short {
		previewHeight -= parseFormHeight
	}
	if m.previewRaw == "" {
		previewHeight = min(previewHeight, 6)
	}
	m.preview.SetHeight(max(1, previewHeight-3))
	m.prompt.SetWidth(max(1, l.width-4))
	m.prompt.SetHeight(min(3, max(1, l.height-8)))
	m.transcript.SetWidth(max(1, l.width-4))
	chatHeight := l.height - m.prompt.Height() - 6
	if l.height < 9 {
		chatHeight = l.height - 3
	}
	if len(m.turns) == 0 {
		chatHeight = min(chatHeight, 3)
	}
	m.transcript.SetHeight(max(1, chatHeight))
	m.details.SetWidth(max(1, l.viewer-4))
	detailHeight := l.height
	if !l.split && !l.short {
		detailHeight -= m.groupRows() + 2
	}
	if len(m.groups) == 0 {
		detailHeight = min(detailHeight, 6)
	}
	m.details.SetHeight(max(1, detailHeight-3))
}

func (m *model) View() tea.View {
	var content string
	var hits []hit
	headerArea := hitArea{hits: &hits, x: 1}
	bodyArea := headerArea.offset(0, 3)
	switch {
	case m.width < 30 || m.height < 10:
		content = fit("PageParse · Ctrl+Q quit", m.width)
	case m.dialog != nil:
		content = " " + m.masthead(m.width-2, headerArea) + "\n\n" + leftPad(m.renderDialog(bodyArea.offset(2, 0)))
	default:
		l := m.layout()
		header := m.masthead(l.width, headerArea) + "\n" + m.connectionLine(l.width) + "\n" + lipLine.Render(strings.Repeat("─", l.width))
		var body string
		switch m.workspace {
		case "chat":
			body = m.chatView(l, bodyArea)
		case "exports":
			body = m.exportsView(l, bodyArea)
		default:
			body = m.parseView(l, bodyArea)
		}
		notice := m.statusText()
		hasStatus := notice != ""
		hint := m.keyHint()
		if notice == "" {
			switch m.workspace {
			case "parse":
				notice = m.parseProgress()
			case "chat":
				notice = "F5 Send   F6 Stop"
			case "exports":
				notice = fmt.Sprintf("%d groups · %d selected", len(m.groups), len(m.selection))
			}
		}
		statusLine := oneLine(notice)
		if hasStatus && (strings.Contains(notice, "\n") || ansi.StringWidth(statusLine) > l.width) {
			const details = "  F10 Details"
			statusLine = fit(statusLine, l.width-len(details)) + details
		}
		if hasStatus {
			headerArea.add(0, m.height-2, ansi.StringWidth(statusLine), 1, statusDetails)
		}
		const helpLabel = "? Help · F11"
		helpX := l.width - ansi.StringWidth(helpLabel)
		hint = fit(hint, helpX-2)
		headerArea.add(helpX, m.height-1, l.width-helpX, 1, helpScreen)
		footer := lipAccent.Render(fit(statusLine, l.width)) + "\n" + lipMuted.Render(hint) +
			strings.Repeat(" ", helpX-ansi.StringWidth(hint)) + lipAccent.Bold(true).Render(helpLabel)
		paddedBody := lipgloss.NewStyle().Height(l.height).MaxHeight(l.height).Render(body)
		content = leftPad(header + "\n" + paddedBody + "\n" + footer)
	}
	view := tea.NewView(content)
	view.AltScreen = true
	view.MouseMode = tea.MouseModeCellMotion
	view.OnMouse = onMouse(hits, m.workspace, m.dialog)
	view.WindowTitle = "PageParse"
	if os.Getenv("NO_COLOR") == "" && os.Getenv("TERM") != "dumb" {
		view.BackgroundColor, view.ForegroundColor = canvas, cream
	}
	return view
}

func leftPad(text string) string { return " " + strings.ReplaceAll(text, "\n", "\n ") }

func fit(text string, width int) string {
	if width < 1 {
		return ""
	}
	return ansi.Truncate(text, width, "…")
}

func (m *model) masthead(width int, area hitArea) string {
	tabs := []struct{ key, title, workspace string }{
		{"F1", "Parse", "parse"}, {"F2", "Chat", "chat"}, {"F3", "Exports", "exports"},
	}
	title := lipText.Bold(true).Render("PageParse")
	items := []string{title, " "}
	if width < 49 {
		items = nil
	}
	x := ansi.StringWidth(strings.Join(items, ""))
	for _, tab := range tabs {
		label := tab.key + " " + tab.title
		if m.dialog == nil && x < width {
			length := min(len(label)+2, width-x)
			*area.hits = append(*area.hits, hit{x: area.x + x, y: area.y, width: length,
				height: 1, workspace: tab.workspace, option: -1})
		}
		x += len(label) + 2
		nameStyle := lipMuted
		if tab.workspace == m.workspace {
			nameStyle = lipAccent.Bold(true)
		}
		items = append(items, " ", lipMuted.Render(tab.key), " ", nameStyle.Render(tab.title), " ")
	}
	return fit(strings.Join(items, ""), width)
}

func (m *model) connectionLine(width int) string {
	var label, text string
	var state connection
	switch m.workspace {
	case "parse":
		label = m.modelID
		if label == "" {
			label = "Nemotron"
		}
		state = m.connections["Nemotron"]
	case "chat":
		label, state = "Ollama", m.connections["Ollama"]
	case "exports":
		text = "Export index · opening"
		if m.indexReady {
			text = "Export index · ready"
		} else if m.feedback["exports"] != "" {
			text = "Export index · attention required"
		}
		return lipMuted.Render(fit(text, width))
	}
	if state.State == "" {
		state.State = "checking"
	}
	details := []string{label, state.State}
	if state.State == "ready" && state.Endpoint != "" {
		details = append(details, state.Endpoint)
	} else if state.Message != "" {
		details = append(details, state.Message)
	}
	return lipMuted.Render(fit(oneLine(strings.Join(details, " · ")), width))
}

func (m *model) keyHint() string {
	switch m.focus {
	case source, pages, destination:
		if m.width >= 63 {
			return "↑↓ Move  Tab Next  F5 Parse  F8 Paste image  F9 Copy"
		}
		return "↑↓ Move  Tab Next  F9 Copy"
	case browse, format, passMode, mdFilters:
		return "↑↓ Move  ↵ Open  Tab Next  F9 Copy"
	case parseStart, parseStop, export:
		return "↑ ← → Move  ↵ Activate  Tab Next"
	case preview, transcript, exportDetails:
		return "↑↓ Scroll  PgUp/PgDn  F9 Copy  Tab Next"
	case previewMode, chatModel, deleteFiles:
		return "↵ Open  ↑↓ Choose  Tab Next  F9 Copy"
	case exportList:
		return "↑↓ Browse  Space Select  Tab Next  F9 Copy"
	}
	return "Tab Next  F9 Copy  Ctrl+Q Quit"
}

func (m *model) parseProgress() string {
	if m.parseStatus == "" {
		return "Select a PDF or image · F8 Paste image"
	}
	status := m.parseStatus
	if m.stopping {
		status = "cancelling"
	}
	if m.total > 0 {
		status += fmt.Sprintf(" · %d/%d pages", m.completed, m.total)
	}
	if m.page != nil {
		status += fmt.Sprintf(" · page %d", *m.page)
	}
	if m.elapsed > 0 {
		status += fmt.Sprintf(" · %.1fs", m.elapsed)
	}
	return "Parse " + status
}

func (m *model) button(id control, label string, primary bool) string {
	style := lipText.Background(surface)
	marker := " "
	switch {
	case id == m.focus && !m.enabled(id):
		style = lipMuted.Underline(true)
		marker = "›"
	case !m.enabled(id):
		style = lipMuted
	case id == m.focus:
		style = lipAction
		marker = "›"
	case primary:
		style = lipAction
	}
	return style.Render(marker + oneLine(label) + " ")
}

func (m *model) input(id control, label string, width int) string {
	marker := "  "
	caption := lipMuted
	if m.focus == id {
		marker = "› "
		caption = lipAccent.Bold(true)
	}
	return fit(caption.Render(fmt.Sprintf("%s%-*s", marker, formLabel, label))+m.inputs[id].View(), width)
}

func (m *model) formView(width, height int, area hitArea) string {
	type formLine struct {
		text string
		hits []hit
	}
	row := func(field control, text string) formLine {
		return formLine{text, []hit{{width: ansi.StringWidth(text), target: field}}}
	}
	buttons := func(controls []control, labels []string) formLine {
		var line formLine
		for index, field := range controls {
			if index > 0 {
				line.text += " "
			}
			button := m.button(field, labels[index], field == parseStart)
			line.hits = append(line.hits, hit{x: ansi.StringWidth(line.text), width: ansi.StringWidth(button), target: field})
			line.text += button
		}
		return line
	}
	browseButton := m.button(browse, "Browse files", false)
	passLabel, _, _ := strings.Cut(choiceLabel(passModes, m.passMode), " · ")
	filterLabel := "Show all"
	if count := len(m.hiddenElements); count > 0 {
		filterLabel = fmt.Sprintf("%d hidden", count)
	}
	lines := []formLine{
		{text: lipText.Bold(true).Render("Document")},
		row(source, m.input(source, "Source", width)),
		{strings.Repeat(" ", formIndent) + browseButton,
			[]hit{{x: formIndent, width: ansi.StringWidth(browseButton), target: browse}}},
		row(pages, m.input(pages, "Pages", width)),
		row(destination, m.input(destination, "Output", width)),
		row(format, "  "+lipMuted.Render(fmt.Sprintf("%-*s", formLabel, "Format"))+m.button(format, choiceLabel(outputFormats, m.format)+" ▾", false)),
		row(passMode, "  "+lipMuted.Render(fmt.Sprintf("%-*s", formLabel, "Pass mode"))+m.button(passMode, passLabel+" ▾", false)),
		row(mdFilters, "  "+lipMuted.Render(fmt.Sprintf("%-*s", formLabel, "Filters"))+m.button(mdFilters, filterLabel+" ▾", false)),
		{},
		buttons([]control{parseStart, parseStop, export}, []string{"Start", "Cancel", "Export"}),
	}
	if m.total > 0 {
		barWidth := min(25, width-2)
		filled := min(barWidth, m.completed*barWidth/m.total)
		lines = append(lines, formLine{text: lipAccent.Render(strings.Repeat("━", filled)) + lipLine.Render(strings.Repeat("━", barWidth-filled))})
	}
	active := 0
	for index, line := range lines {
		for _, region := range line.hits {
			if region.target == m.focus {
				active = index
			}
		}
	}
	if m.total > 0 && active == len(lines)-2 && height > 1 {
		active++
	}
	offset := min(max(0, active-height+1), max(0, len(lines)-height))
	var visible []string
	for index, line := range lines[offset:min(len(lines), offset+height)] {
		visible = append(visible, fit(line.text, width))
		for _, region := range line.hits {
			area.add(region.x, index, min(region.width, width-region.x), 1, region.target)
		}
	}
	return strings.Join(visible, "\n")
}

func (m *model) parseView(l layout, area hitArea) string {
	isViewer := m.focus == previewMode || m.focus == preview
	if l.short && !l.split && !isViewer {
		return m.formView(l.form, l.height, area)
	}
	viewerArea := area
	if l.split {
		viewerArea = area.offset(l.form+2, 0)
	} else if !l.short {
		viewerArea = area.offset(0, parseFormHeight)
	}
	label := m.button(previewMode, choiceLabel(previewModes, m.previewMode)+" ▾", false)
	viewerArea.add(0, 0, l.viewer, m.preview.Height()+3, preview)
	viewerArea.text(11, 1, label, previewMode)
	if count := m.completed; count > 0 && l.viewer > 43 {
		pagesLabel := "pages"
		if count == 1 {
			pagesLabel = "page"
		}
		label += lipDarkMuted.Render(fmt.Sprintf("  %d %s", count, pagesLabel))
	}
	viewer := m.viewer("Preview", label, m.preview, isViewer)
	if l.split {
		form := m.formView(l.form, l.height, area)
		return lipgloss.JoinHorizontal(lipgloss.Top, lipgloss.NewStyle().Width(l.form).Render(form), "  ", viewer)
	}
	if l.short {
		return viewer
	}
	form := lipgloss.NewStyle().Height(parseFormHeight).Render(m.formView(l.form, parseFormHeight, area))
	return form + "\n" + viewer
}

func (m *model) chatView(l layout, area hitArea) string {
	modelLabel := m.chatID
	if modelLabel == "" {
		modelLabel = "Choose model"
	}
	modelLabel = fit(oneLine(modelLabel), max(9, l.width-15))
	modelButton := m.button(chatModel, modelLabel+" ▾", false)
	newButton := m.button(newChat, "New chat", false)
	bar := modelButton + " " + newButton
	reply := m.viewer("Conversation", "", m.transcript, m.focus == transcript)
	replyHeight := lipgloss.Height(reply)
	if l.height < 9 && m.focus == transcript {
		area.add(0, 0, l.width, replyHeight, transcript)
		return reply
	}
	area.text(0, 0, modelButton, chatModel)
	area.text(ansi.StringWidth(modelButton)+1, 0, newButton, newChat)

	promptLine := lipMuted.Render("Message")
	if m.focus == prompt {
		promptLine = lipAccent.Bold(true).Render("› Message")
	}
	promptBox := lipgloss.NewStyle().PaddingLeft(2).Render(m.prompt.View())
	sendButton := m.button(send, "Send", true)
	stopButton := m.button(chatStop, "Stop", false)
	composer := promptLine + "\n" + promptBox + "\n" + sendButton + " " + stopButton
	offset := 1
	if l.height >= 9 {
		area.add(0, 1, l.width, replyHeight, transcript)
		offset += replyHeight
	}
	area.add(0, offset, l.width, m.prompt.Height()+1, prompt)
	offset += m.prompt.Height() + 1
	area.text(0, offset, sendButton, send)
	area.text(ansi.StringWidth(sendButton)+1, offset, stopButton, chatStop)
	if l.height < 9 {
		return fit(bar, l.width) + "\n" + composer
	}
	return fit(bar, l.width) + "\n" + reply + "\n" + composer
}

func (m *model) groupRows() int {
	l := m.layout()
	if l.split || l.short {
		return max(1, l.height-2)
	}
	return min(5, max(1, len(m.groups)))
}

func listOffset(index, length, rows int) int {
	return min(max(0, index-rows/2), max(0, length-rows))
}

func (m *model) exportsView(l layout, area hitArea) string {
	rows := m.groupRows()
	isDetails := m.focus == exportDetails
	viewer := m.viewer("Files", "", m.details, isDetails)
	if isDetails && !l.split && l.short {
		area.add(0, 0, l.viewer, m.details.Height()+3, exportDetails)
		return viewer
	}
	lines := []string{lipText.Bold(true).Render("Export groups")}
	if len(m.groups) == 0 {
		lines = append(lines, lipMuted.Render("No exports saved yet."))
	} else {
		start := listOffset(m.groupIndex, len(m.groups), rows)
		for index := start; index < min(len(m.groups), start+rows); index++ {
			item := m.groups[index]
			check := "□"
			if m.selection[item.ID] {
				check = "■"
			}
			label := " " + check + " " + oneLine(item.Source)
			if index == m.groupIndex {
				style := lipText.Bold(true).Background(surface)
				if m.focus == exportList {
					style = lipAction
				}
				label = style.Render(fit(label, l.form))
			}
			*area.hits = append(*area.hits, hit{x: area.x, y: area.y + len(lines), width: l.form,
				height: 1, target: exportList, groupID: item.ID, option: index})
			lines = append(lines, fit(label, l.form))
		}
	}
	deleteButton := m.button(deleteFiles, "Delete selected ▾", false)
	area.text(0, len(lines), deleteButton, deleteFiles)
	lines = append(lines, deleteButton)
	groups := strings.Join(lines, "\n")
	if l.split {
		area.add(l.form+2, 0, l.viewer, m.details.Height()+3, exportDetails)
		return lipgloss.JoinHorizontal(lipgloss.Top, lipgloss.NewStyle().Width(l.form).Render(groups), "  ", viewer)
	}
	if l.short {
		return groups
	}
	area.add(0, len(lines), l.viewer, m.details.Height()+3, exportDetails)
	return groups + "\n" + viewer
}

func (m *model) viewer(title, action string, body viewport.Model, focused bool) string {
	borderColor := lipgloss.Color("#60574f")
	if focused {
		borderColor = coral
	}
	heading := lipDark.Bold(true).Render(title)
	if action != "" {
		heading = lipgloss.JoinHorizontal(lipgloss.Top, heading, lipDark.Render("  "), action)
	}
	width := max(1, body.Width())
	lines := strings.Split(body.View(), "\n")
	for index, line := range lines {
		lines[index] = paintLine(line, width, lipDark)
	}
	text := paintLine(heading, width, lipDark) + "\n" + strings.Join(lines, "\n")
	return lipgloss.NewStyle().Background(dark).Border(lipgloss.RoundedBorder()).
		BorderForeground(borderColor).BorderBackground(dark).Padding(0, 1).Render(text)
}

func paintLine(line string, width int, style lipgloss.Style) string {
	line = fit(line, width)
	fill := max(0, width-ansi.StringWidth(line))
	out := style.Render(line + strings.Repeat(" ", fill))
	// Restore the panel colors after nested styles reset the terminal attributes.
	reset := ansi.ResetStyle
	out = strings.ReplaceAll(out, "\x1b[0m", reset)
	if strings.HasSuffix(out, reset) {
		body := strings.TrimSuffix(out, reset)
		prefix, _, _ := strings.Cut(style.Render("!"), "!")
		out = strings.ReplaceAll(body, reset, reset+prefix) + reset
	}
	return out
}

func (m *model) dialogWidth() int {
	if m.dialog != nil && m.dialog.kind == helpScreen {
		return max(12, m.width-6)
	}
	return min(72, max(12, m.width-6))
}
func (m *model) dialogRows() int { return max(1, m.height-8) }

func (m *model) dialogDetailLines(d *dialog) []string {
	detail := strings.ReplaceAll(safeText(d.detail), "\t", "    ")
	if d.kind == helpScreen {
		return strings.Split(ansi.Wrap(detail, m.dialogWidth()-4, ""), "\n")
	}
	return strings.Split(ansi.Hardwrap(detail, m.dialogWidth()-4, true), "\n")
}
func (m *model) dialogDetailMaxOffset(d *dialog) int {
	return max(0, len(m.dialogDetailLines(d))-m.dialogRows())
}

func (m *model) renderDialog(area hitArea) string {
	d := m.dialog
	width := m.dialogWidth() - 4
	lines := []string{lipText.Bold(true).Render(fit(d.title, width))}
	if d.subtitle != "" {
		lines = append(lines, lipMuted.Render(fit(oneLine(d.subtitle), width)))
	}
	rows := m.dialogRows()
	if d.action != "" {
		details := m.dialogDetailLines(d)
		start := min(d.offset, m.dialogDetailMaxOffset(d))
		detailRows := details[start:min(len(details), start+rows)]
		area.add(0, len(lines), width, len(detailRows), dialogBody)
		lines = append(lines, detailRows...)
		back := " Back "
		if d.action == "Close" {
			back = " Close "
		}
		accept := " " + d.action + " "
		plain := lipText.Background(dark)
		backButton, acceptButton := lipAction.Render(back), plain.Render(accept)
		if d.confirmFocused && d.action != "Close" {
			backButton, acceptButton = plain.Render(back), lipAction.Render(accept)
		}
		area.text(0, len(lines), backButton, dialogBack)
		actions := backButton
		hint := "Esc"
		if d.kind == helpScreen {
			hint = "Esc  ↑↓ Scroll  PgUp/PgDn"
		}
		if d.action != "Close" {
			area.text(ansi.StringWidth(backButton)+1, len(lines), acceptButton, dialogAccept)
			actions += " " + acceptButton
			hint = "←→  ↵"
		}
		lines = append(lines, fit(actions+"   "+lipMuted.Render(hint+"   F9 Copy"), width))
	} else {
		start := listOffset(d.cursor, len(d.choices), rows)
		for index := start; index < min(len(d.choices), start+rows); index++ {
			item := d.choices[index]
			opt := oneLine(item.label)
			if d.kind == mdFilters {
				check := "[ ] "
				if m.hiddenElements[item.value] {
					check = "[x] "
				}
				opt = check + opt
			}
			*area.hits = append(*area.hits, hit{x: area.x, y: area.y + len(lines), width: width,
				height: 1, target: dialogBody, option: index})
			if index == d.cursor {
				lines = append(lines, lipAction.Render(fit("› "+opt, width)))
			} else {
				lines = append(lines, fit("  "+opt, width))
			}
		}
		if len(d.choices) == 0 {
			lines = append(lines, "No documents in this folder.")
		}
		hint := "↑↓ Move   ↵ Choose   Esc Back"
		if d.kind == mdFilters {
			hint = "Space Toggle  Esc Done"
		}
		if d.kind == browse && width >= 41 {
			hint = "↑↓ Move   ↵ Open   ← Parent   ~ Home   Esc Back"
		}
		lines = append(lines, lipMuted.Render(fit(hint, width)))
	}
	for index, line := range lines {
		lines[index] = paintLine(line, width, lipText.Background(surface))
	}
	return lipgloss.NewStyle().Background(surface).Border(lipgloss.RoundedBorder()).
		BorderForeground(coral).BorderBackground(surface).Padding(0, 1).Render(strings.Join(lines, "\n"))
}

func (m *model) updatePreview() {
	content := strings.ReplaceAll(safeText(m.previewRaw), "\t", "    ")
	if content == "" {
		content = lipDarkMuted.Render("Parsed pages will appear here.")
	}
	m.preview.SetContent(content)
	m.resize()
}

type markdownRendered struct {
	revision, width int
	text            string
}

func (m *model) queueRender() tea.Cmd {
	width := m.preview.Width()
	if m.previewMode != "rendered" || m.previewRaw == "" || len(m.previewRaw) > 2*1024*1024 {
		return nil
	}
	if m.renderBusy || (m.renderRaw == m.previewRaw && m.renderWidth == width) {
		return nil
	}
	m.renderBusy, m.renderRaw, m.renderWidth = true, m.previewRaw, width
	revision, raw := m.previewRevision, m.previewRaw
	return func() tea.Msg {
		style := []byte(fmt.Sprintf(`{
			"document":{"color":%q,"margin":0},
			"heading":{"color":"#e8a55a","bold":true,"block_suffix":"\n"},
			"link":{"color":"#5db8a6","underline":true},
			"strong":{"bold":true},"emph":{"italic":true},
			"hr":{"format":"────────"},
			"item":{"block_prefix":"• "},"enumeration":{"block_prefix":". "},
			"table":{"center_separator":"┼","column_separator":"│","row_separator":"─"},
			"code":{"color":"#e8a55a"},"code_block":{"color":%q}
		}`, textHex, textHex))
		renderer, err := glamour.NewTermRenderer(glamour.WithStylesFromJSONBytes(style),
			glamour.WithWordWrap(width), glamour.WithTableWrap(true))
		if err != nil {
			return markdownRendered{revision: revision, width: width}
		}
		text, err := renderer.Render(safeText(raw))
		if err != nil {
			return markdownRendered{revision: revision, width: width}
		}
		return markdownRendered{revision, width, strings.Trim(text, "\n")}
	}
}

func (m *model) transcriptText(styled bool) string {
	var output strings.Builder
	for index, turn := range m.turns {
		if index > 0 {
			output.WriteString("\n\n")
		}
		assistant := "Assistant"
		if turn.state != "complete" {
			assistant += " · " + turn.state
		}
		user := "You"
		if styled {
			user, assistant = lipDarkAccent.Render(user), lipDarkAccent.Render(assistant)
		}
		output.WriteString(user + "\n" + safeText(turn.prompt) + "\n\n" + assistant + "\n" + safeText(turn.reply))
	}
	return output.String()
}

func (m *model) updateTranscript(follow bool) {
	content := m.transcriptText(true)
	if content == "" {
		content = lipDarkMuted.Render("Send a message to begin.")
	}
	m.transcript.SetContent(content)
	m.resize()
	if follow {
		m.transcript.GotoBottom()
	}
}

func (m *model) updateExportDetails(reset bool) {
	content := m.groupText()
	if content == "" {
		content = lipDarkMuted.Render("Saved files will appear here.")
	}
	m.details.SetContent(content)
	m.resize()
	if reset {
		m.details.GotoTop()
	}
}
