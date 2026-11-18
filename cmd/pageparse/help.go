package main

func (m *model) toggleHelp() {
	if m.dialog != nil && m.dialog.kind == helpScreen {
		m.closeDialog()
		return
	}
	m.dialog = &dialog{kind: helpScreen, title: "PageParse Help", detail: helpText,
		action: "Close", underlying: m.dialog}
}

const helpText = `WORKSPACES AND SHORTCUTS
F1  Parse PDFs and images
F2  Chat with an Ollama model
F3  View and manage exports
F4  Refresh server status and the export index
F5  Start parsing in Parse or send a message in Chat
F6  Cancel parsing or stop the chat reply
F7  Export the current parse result (in Parse)
F8  Paste an image from the clipboard (in Parse)
F9  Copy all preview text, conversation, export details, or dialog text
F10  Open the full status message when one is available
? (Shift+/ on a US keyboard) / F11  Open or close Help
Ctrl+Q  Quit PageParse

NAVIGATION
Tab / Shift+Tab  Focus the next or previous control
Enter / Space  Activate a focused button or open a menu
↑ / ↓  Move in an open menu
Page Up / Page Down  Move one page in a menu
Home / End  Jump to the first or last choice
Enter / Space  Choose the highlighted item
Esc  Close the menu or Help and return to the previous screen
Mouse  Click tabs, controls or choices. Click ? Help to open Help.
Mouse wheel / ↑ / ↓ / Page Up / Page Down  Scroll Help
Home / End  Jump to the beginning or end of Help

PARSE
↑ / ↓  Move between Source, Browse files, Pages, Output, Format and Start
← / →  Move between Start, Cancel and Export on the action row
↑  Return from the action row to Format
Source  Enter a path, paste it, or choose Browse files. Supports PDF, PNG, JPEG and WebP.
Browse files  Enter, Space or → opens the selected folder or file. ← or Backspace goes to the parent folder. ~ goes to your home folder.
Pages  Enter all, leave it blank, or enter a selection such as 1,3-5.
Output  Enter the export directory.
Format  Press Enter, then use ↑ / ↓ to choose Markdown + JSON, Markdown or JSON.
Preview  Use Tab or click to focus the preview or its mode menu. Choose Rendered, Markdown or JSON.
Ctrl+V in Source  Paste a clipboard image or fall back to clipboard text.

CHAT
Model  Choose an installed Ollama model. Switching models clears the conversation.
New chat  Clear the conversation.
Message  Enter inserts a line break. Use F5 or Send to send your message.
Stop  Stop the reply and keep the text received so far.

EXPORTS
↑ / ↓ / Page Up / Page Down / Home / End  Browse export groups when the list has focus
Space / Enter  Toggle selection for the focused group. Click a group to toggle it.
Details  Focus the details pane to scroll file names, formats, sizes and paths.
Delete  Choose the file formats to delete, review the paths, then confirm.
Confirmations  ← / → or Tab / Shift+Tab moves between Back and the action. Enter activates the focused choice. Esc goes back. Use arrows, Page Up, Page Down, Home, End or the wheel to scroll the details.

READING AND COPYING
Scroll preview, conversation or export details with ↑ / ↓, Page Up / Page Down or the mouse wheel. You can also use k / j for lines, b / f or Space for pages, and u / d or Ctrl+U / Ctrl+D for half pages.
Ctrl+C  Copy the focused input if it contains text. Otherwise, copy the same content as F9.
F9 copies full text, including off-screen content. Rendered preview copies the original Markdown.
Shift+drag  Select visible text in terminals that support bypassing app mouse handling.

TEXT EDITING
← / → or Ctrl+B / Ctrl+F  Move by character
Alt+← / Alt+→ or Alt+B / Alt+F  Move by word. Ctrl+← / Ctrl+→ also works in Parse fields.
Home / End or Ctrl+A / Ctrl+E  Move to the start or end of the line
Backspace / Delete or Ctrl+H / Ctrl+D  Delete a character
Alt+Backspace or Ctrl+W  Delete the previous word
Alt+Delete or Alt+D  Delete the next word
Ctrl+U / Ctrl+K  Delete to the start or end of the line
Ctrl+V  Paste from the clipboard
Alt+?  Insert a question mark in a text field
Chat Message  ↑ / ↓ or Ctrl+P / Ctrl+N moves by line. Page Up / Page Down moves by page.
Chat Message  Ctrl+Home / Ctrl+End or Alt+< / Alt+> moves to the start or end of the message.
Chat Message  Alt+C / Alt+L / Alt+U capitalizes, lowercases or uppercases the next word. Ctrl+T swaps the preceding characters.`
