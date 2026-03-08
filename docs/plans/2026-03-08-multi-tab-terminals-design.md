# Multi-Tab Terminal Support

**Date:** 2026-03-08
**Status:** Approved

## Overview

Add browser-style tabs to the web terminal, where each tab owns its own independent split-pane layout. This lets users run multiple concurrent sessions (e.g., Claude in one tab, logs in another, git in a third) without losing the existing split-pane feature.

## Architecture

```
┌─[Shell 1]──[Shell 2]──[+ ]──────────────────────────────────┐
│                                                               │
│  Tab 1's pane container (hidden when tab inactive)            │
│  ┌─────────────────────┐ | ┌─────────────────────┐           │
│  │     Pane 1           │ | │     Pane 2           │          │
│  └─────────────────────┘ | └─────────────────────┘           │
│                                                               │
│  Tab 2's pane container (display:none when inactive)          │
│  ┌─────────────────────────────────────────────────┐          │
│  │     Pane 1 (full width)                          │          │
│  └─────────────────────────────────────────────────┘          │
└───────────────────────────────────────────────────────────────┘
```

## Data Model

```javascript
tabs = [
  {
    id: "tab-1",
    label: "Shell 1",           // double-click to rename
    panes: [                    // each tab owns 1-2 panes
      { id, element, term, fitAddon, searchAddon, sessionId }
    ],
    activePaneId: "pane-1",
    paneContainer: <div>,       // per-tab container element
    divider: <div> | null       // per-tab divider (if split)
  }
]
activeTabId = "tab-1"
```

## Constraints

- **Max 5 tabs** (up to 2 panes each = max 10 PTY sessions)
- **Default label:** "Shell N" — double-click to rename
- **No persistence:** tabs and sessions are lost on page refresh
- **Backend unchanged:** tabs are purely frontend; each pane still calls `/api/session`

## Tab Bar UI

- 32px height, positioned above the pane container
- Translucent/blurred background matching existing toolbar aesthetic
- Theme-aware (adapts to light/dark)
- Each tab shows: label + close "x" (visible on hover or when active)
- "+" button at end (hidden when 5 tabs reached)
- Active tab has a subtle bottom border accent

## Keyboard Shortcuts

### Tab shortcuts (new)
| Shortcut | Action |
|----------|--------|
| `Ctrl+Shift+T` | New tab |
| `Ctrl+Shift+[` | Previous tab |
| `Ctrl+Shift+]` | Next tab |
| `Ctrl+Shift+1-5` | Jump to tab by number |

### Pane shortcuts (moved from Ctrl+Shift to Alt+Shift)
| Shortcut | Action |
|----------|--------|
| `Alt+Shift+D` | Split pane within active tab |
| `Alt+Shift+W` | Close pane (closes tab if last pane) |
| `Alt+Shift+[` | Previous pane within tab |
| `Alt+Shift+]` | Next pane within tab |

## Tab Lifecycle

1. **Create:** "+" button or `Ctrl+Shift+T`. Creates tab with one pane, spawns PTY, switches to it.
2. **Switch:** Click tab or `Ctrl+Shift+[/]`. Hides current container, shows target, refits panes, focuses active pane.
3. **Rename:** Double-click label, inline edit, Enter to confirm, Escape to cancel.
4. **Close:** Click "x" or close last pane via `Alt+Shift+W`. Terminates all PTY sessions in that tab. If last tab, auto-creates a new "Shell 1".

## Implementation Scope

### Modified files
- `static/index.html` — tab bar HTML/CSS, JS refactored to wrap panes inside tabs

### Unchanged files
- `app.py` — backend has no tab concept
- `static/poll-worker.js` — already supports multiple panes by paneId

### Estimated changes
- ~40 lines CSS (tab bar styling)
- ~150 lines net JS change (tab management functions, refactored pane logic)

## Out of Scope (YAGNI)
- Drag-to-reorder tabs
- Tab persistence across page reloads
- Tab-specific themes or settings
- Session reconnection / PTY resumption (separate project)
