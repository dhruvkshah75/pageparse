package main

import (
	"slices"

	tea "charm.land/bubbletea/v2"
	"github.com/charmbracelet/x/ansi"
)

type hit struct {
	x, y, width, height int
	target              control
	groupID             string
	workspace           string
	option              int
}

type pointerAction struct {
	hit
	workspaceAtRender string
	dialog            *dialog
	scroll            int
}

type hitArea struct {
	hits *[]hit
	x, y int
}

func (a hitArea) offset(x, y int) hitArea { return hitArea{a.hits, a.x + x, a.y + y} }

func (a hitArea) add(x, y, w, h int, target control) {
	*a.hits = append(*a.hits, hit{x: a.x + x, y: a.y + y, width: w, height: h, target: target, option: -1})
}

func (a hitArea) text(x, y int, text string, target control) {
	a.add(x, y, ansi.StringWidth(text), 1, target)
}

func onMouse(hits []hit, workspace string, modal *dialog) func(tea.MouseMsg) tea.Cmd {
	return func(msg tea.MouseMsg) tea.Cmd {
		mouse := msg.Mouse()
		scroll := 0
		switch msg.(type) {
		case tea.MouseClickMsg:
			if mouse.Button != tea.MouseLeft {
				return nil
			}
		case tea.MouseWheelMsg:
			switch mouse.Button {
			case tea.MouseWheelUp:
				scroll = -3
			case tea.MouseWheelDown:
				scroll = 3
			default:
				return nil
			}
		default:
			return nil
		}
		for index := len(hits) - 1; index >= 0; index-- {
			h := hits[index]
			if mouse.X >= h.x && mouse.X < h.x+h.width && mouse.Y >= h.y && mouse.Y < h.y+h.height {
				return func() tea.Msg { return pointerAction{h, workspace, modal, scroll} }
			}
		}
		return nil
	}
}

const (
	dialogBody    control = "dialog body"
	dialogAccept  control = "dialog accept"
	dialogBack    control = "dialog back"
	statusDetails control = "status details"
)

func (m *model) usePointer(action pointerAction) tea.Cmd {
	if m.workspace != action.workspaceAtRender || m.dialog != action.dialog {
		return nil
	}
	if d := m.dialog; d != nil {
		if action.scroll != 0 {
			if d.action != "" {
				d.offset = min(max(0, d.offset+action.scroll), m.dialogDetailMaxOffset(d))
			} else if len(d.choices) > 0 {
				d.cursor = min(max(0, d.cursor+action.scroll), len(d.choices)-1)
			}
		} else {
			switch action.target {
			case dialogAccept:
				d.confirmFocused = true
				return m.dialogKey("enter")
			case dialogBack:
				return m.dialogKey("esc")
			case dialogBody:
				if action.option >= 0 && action.option < len(d.choices) {
					m.choose(d, d.choices[action.option])
				}
			}
		}
		return nil
	}
	if action.scroll != 0 {
		switch action.target {
		case preview, previewMode:
			m.preview.SetYOffset(m.preview.YOffset() + action.scroll)
		case transcript:
			m.transcript.SetYOffset(m.transcript.YOffset() + action.scroll)
		case exportDetails:
			m.details.SetYOffset(m.details.YOffset() + action.scroll)
		case exportList:
			next := max(0, min(m.groupIndex+action.scroll, len(m.groups)-1))
			if next != m.groupIndex {
				m.groupIndex = next
				m.updateExportDetails(true)
			}
		}
		return nil
	}
	if action.workspace != "" {
		return m.switchWorkspace(action.workspace)
	}
	if action.target == helpScreen {
		m.toggleHelp()
		return nil
	}
	if action.target == statusDetails {
		m.openStatus()
		return nil
	}
	if action.target == exportList {
		index := slices.IndexFunc(m.groups, func(group exportGroup) bool { return group.ID == action.groupID })
		if index < 0 {
			return nil
		}
		m.groupIndex = index
		m.updateExportDetails(true)
	}
	return tea.Batch(m.setFocus(action.target), m.activate())
}
