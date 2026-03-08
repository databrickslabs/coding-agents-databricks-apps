# Multi-Tab Terminals Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add browser-style tabs to the web terminal where each tab owns its own independent split-pane layout, with renamable labels and a 5-tab cap.

**Architecture:** Purely frontend change to `static/index.html`. The existing flat `panes[]` array and `activePaneId` get wrapped inside a `tabs[]` array. Each tab owns a dedicated pane container DOM element. Switching tabs toggles CSS `display` on these containers. The backend and poll-worker are unchanged.

**Tech Stack:** Vanilla JS, xterm.js, existing CSS conventions (translucent/blurred, theme-aware)

---

### Task 1: Add Tab Bar HTML and CSS

**Files:**
- Modify: `static/index.html:14-17` (pane container CSS)
- Modify: `static/index.html:199-255` (HTML body, before pane-container)

**Step 1: Add tab bar CSS**

Insert after line 12 (`#status` rule) and before line 14 (`/* Pane container */`):

```css
    /* Tab bar */
    #tab-bar {
      display: flex; align-items: center; height: 32px; width: 100vw;
      background: rgba(255,255,255,0.04);
      border-bottom: 1px solid rgba(255,255,255,0.08);
      backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
      overflow-x: auto; overflow-y: hidden;
      user-select: none; flex-shrink: 0;
    }
    .tab {
      display: flex; align-items: center; gap: 6px;
      padding: 0 12px; height: 100%; cursor: pointer;
      font-size: 12px; white-space: nowrap;
      border-right: 1px solid rgba(255,255,255,0.06);
      transition: background 0.15s;
      position: relative;
    }
    .tab:hover { background: rgba(255,255,255,0.06); }
    .tab.active {
      background: rgba(255,255,255,0.08);
      border-bottom: 2px solid rgba(100,150,255,0.6);
    }
    .tab-label {
      outline: none; border: none; background: none;
      color: inherit; font: inherit; padding: 0;
      min-width: 30px; max-width: 120px;
      cursor: inherit;
    }
    .tab-label:focus {
      cursor: text;
      border-bottom: 1px solid rgba(100,150,255,0.5);
    }
    .tab-close {
      opacity: 0; font-size: 10px; padding: 2px 4px;
      border-radius: 3px; border: none; background: none;
      color: inherit; cursor: pointer; transition: opacity 0.15s, background 0.15s;
      line-height: 1;
    }
    .tab:hover .tab-close, .tab.active .tab-close { opacity: 0.6; }
    .tab-close:hover { opacity: 1 !important; background: rgba(255,255,255,0.1); }
    #new-tab-btn {
      padding: 0 10px; height: 100%; border: none; background: none;
      color: inherit; font-size: 16px; cursor: pointer;
      opacity: 0.5; transition: opacity 0.15s;
    }
    #new-tab-btn:hover { opacity: 1; }
    #new-tab-btn:disabled { opacity: 0.2; cursor: default; }
```

**Step 2: Update pane container height**

Change line 15 from:
```css
    #pane-container { display: flex; flex-direction: row; height: 100vh; width: 100vw; }
```
to:
```css
    .tab-pane-container { display: flex; flex-direction: row; height: calc(100vh - 33px); width: 100vw; }
    .tab-pane-container.hidden { display: none; }
```

Note: The old `#pane-container` ID is no longer used. Each tab creates its own `.tab-pane-container` div dynamically.

**Step 3: Update pane divider CSS**

Change line 20-27 from `#pane-divider` to class-based:
```css
    .pane-divider {
      flex: 0 0 4px; cursor: col-resize;
      background: rgba(128,128,128,0.15);
      transition: background 0.15s;
      z-index: 1;
    }
    .pane-divider:hover, .pane-divider.dragging {
      background: rgba(100,150,255,0.5);
    }
```

**Step 4: Add tab bar HTML**

Replace line 255 (`<div id="pane-container"></div>`) with:
```html
  <div id="tab-bar">
    <button id="new-tab-btn" title="New tab (Ctrl+Shift+T)">+</button>
  </div>
  <!-- Tab pane containers are created dynamically by createTab() -->
```

**Step 5: Verify the page loads without errors**

Open in browser, confirm the tab bar strip renders (empty, with just the "+" button). Terminal won't work yet because the JS still references the old `#pane-container`.

**Step 6: Commit**

```bash
git add static/index.html
git commit -m "feat: add tab bar HTML and CSS"
```

---

### Task 2: Refactor State Model — Introduce Tabs Array

**Files:**
- Modify: `static/index.html:356-383` (State and Pane Object Model sections)

**Step 1: Replace flat pane state with tabs model**

Replace the Pane Object Model section (lines 367-383):
```javascript
    // ── Pane Object Model ─────────────────────────────────────────
    // Each pane: { id, element, term, fitAddon, searchAddon, sessionId }
    let panes = [];
    let activePaneId = null;
    let paneIdCounter = 0;

    function getActivePane() {
      return panes.find(p => p.id === activePaneId) || panes[0];
    }

    function focusPane(id) {
      activePaneId = id;
      panes.forEach(p => {
        p.element.classList.toggle('active', p.id === id);
        if (p.id === id) p.term.focus();
      });
    }
```

With:
```javascript
    // ── Tab & Pane Object Model ───────────────────────────────────
    // Tab: { id, label, panes[], activePaneId, paneContainer, divider }
    // Pane: { id, element, term, fitAddon, searchAddon, sessionId }
    const MAX_TABS = 5;
    let tabs = [];
    let activeTabId = null;
    let tabIdCounter = 0;
    let paneIdCounter = 0;

    function getActiveTab() {
      return tabs.find(t => t.id === activeTabId) || tabs[0];
    }

    function getActivePane() {
      const tab = getActiveTab();
      if (!tab) return null;
      return tab.panes.find(p => p.id === tab.activePaneId) || tab.panes[0];
    }

    function getAllPanes() {
      return tabs.flatMap(t => t.panes);
    }

    function focusPane(id) {
      const tab = getActiveTab();
      if (!tab) return;
      tab.activePaneId = id;
      tab.panes.forEach(p => {
        p.element.classList.toggle('active', p.id === id);
        if (p.id === id) p.term.focus();
      });
    }
```

**Step 2: Verify no syntax errors**

Page will be broken (functions reference old `panes` global). That's expected — we fix the references in the next tasks.

**Step 3: Commit**

```bash
git add static/index.html
git commit -m "refactor: introduce tabs array data model"
```

---

### Task 3: Update Theme, Font, and Refit Functions for Tabs

**Files:**
- Modify: `static/index.html` — `applyTheme`, `setFontSize`, `setFontFamily`, `refitAllPanes` functions

**Step 1: Update applyTheme**

Replace line 403 (`panes.forEach(...)`) with:
```javascript
      getAllPanes().forEach(p => { p.term.options.theme = preset.theme; });
```

**Step 2: Update setFontSize**

Replace line 424 (`panes.forEach(...)`) with:
```javascript
      getAllPanes().forEach(p => { p.term.options.fontSize = currentFontSize; });
```

**Step 3: Update setFontFamily**

Replace line 434 (`panes.forEach(...)`) with:
```javascript
      getAllPanes().forEach(p => { p.term.options.fontFamily = family; });
```

**Step 4: Update refitAllPanes to only refit the active tab's panes**

Replace the `refitAllPanes` function:
```javascript
    function refitAllPanes() {
      const tab = getActiveTab();
      if (!tab) return;
      tab.panes.forEach(p => {
        p.fitAddon.fit();
        if (p.sessionId) sendResize(p.term.cols, p.term.rows, p.sessionId);
      });
    }
```

**Step 5: Commit**

```bash
git add static/index.html
git commit -m "refactor: update theme/font/refit to use tabs model"
```

---

### Task 4: Rewrite createPane to Accept a Parent Tab

**Files:**
- Modify: `static/index.html` — `createPane` function (lines ~773-838)

**Step 1: Rewrite createPane**

Replace the entire `createPane` function with:
```javascript
    async function createPane(tab) {
      const id = 'pane-' + (++paneIdCounter);
      const container = tab.paneContainer;
      const element = document.createElement('div');
      element.className = 'pane';
      element.id = id;

      // Add divider before second pane
      if (tab.panes.length === 1) {
        const divider = document.createElement('div');
        divider.className = 'pane-divider';
        container.appendChild(divider);
        tab.divider = divider;
        setupDividerDrag(divider, tab);
      }

      container.appendChild(element);

      const term = new Terminal({
        cursorBlink: true,
        fontSize: currentFontSize,
        fontFamily: fontFamilies[currentFontFamily] || 'monospace',
        theme: themes[currentThemeName].theme
      });

      const fitAddon = new FitAddon.FitAddon();
      term.loadAddon(fitAddon);
      term.loadAddon(new WebLinksAddon.WebLinksAddon());

      let searchAddon = null;
      if (typeof SearchAddon !== 'undefined') {
        searchAddon = new SearchAddon.SearchAddon();
        term.loadAddon(searchAddon);
      }

      if (typeof ImageAddon !== 'undefined' && ImageAddon.ImageAddon) {
        term.loadAddon(new ImageAddon.ImageAddon({
          sixelSupport: true,
          sixelScrolling: true,
          iipSupport: true,
          enableSizeReports: true,
          storageLimit: 128
        }));
      }

      term.open(element);
      fitAddon.fit();

      const sid = await createSession();
      await sendResize(term.cols, term.rows, sid);

      term.write('\x1b[32mConnected. Type "claude" to start coding.\x1b[0m\r\n');
      term.write('\x1b[90mProjects in ~/projects auto-sync to Workspace on git commit.\x1b[0m\r\n');
      term.write('\x1b[90mCtrl+Shift+T new tab \u2502 Alt+Shift+D split pane \u2502 Alt+Shift+W close pane\x1b[0m\r\n\r\n');

      const pane = { id, element, term, fitAddon, searchAddon, sessionId: sid };
      term.onData(data => sendInput(data, pane.sessionId));
      pollWorker.postMessage({ type: 'start_poll', paneId: id, sessionId: sid });

      // Click to focus
      element.addEventListener('mousedown', () => focusPane(id));

      tab.panes.push(pane);
      focusPane(id);

      return pane;
    }
```

**Step 2: Commit**

```bash
git add static/index.html
git commit -m "refactor: createPane now accepts parent tab"
```

---

### Task 5: Implement Tab Management Functions (createTab, switchTab, closeTab, renameTab)

**Files:**
- Modify: `static/index.html` — add new functions after createPane

**Step 1: Add createTab function**

Insert after the `createPane` function:
```javascript
    // ── Tab Management ──────────────────────────────────────────────
    async function createTab() {
      if (tabs.length >= MAX_TABS) return null;

      const id = 'tab-' + (++tabIdCounter);
      const label = 'Shell ' + tabIdCounter;

      // Create per-tab pane container
      const paneContainer = document.createElement('div');
      paneContainer.className = 'tab-pane-container';
      paneContainer.id = id + '-panes';
      document.body.appendChild(paneContainer);

      const tab = {
        id,
        label,
        panes: [],
        activePaneId: null,
        paneContainer,
        divider: null
      };

      tabs.push(tab);

      // Render tab in the tab bar
      renderTabBar();

      // Switch to new tab (hides others)
      switchTab(id);

      // Create first pane
      await createPane(tab);

      updateTabButtons();
      return tab;
    }

    function switchTab(id) {
      const prevTab = getActiveTab();
      activeTabId = id;

      // Toggle pane container visibility
      tabs.forEach(t => {
        t.paneContainer.classList.toggle('hidden', t.id !== id);
      });

      // Update tab bar active state
      renderTabBar();

      // Refit panes in the newly visible tab and focus
      const tab = getActiveTab();
      if (tab && tab.panes.length > 0) {
        requestAnimationFrame(() => {
          refitAllPanes();
          const ap = tab.panes.find(p => p.id === tab.activePaneId) || tab.panes[0];
          if (ap) ap.term.focus();
        });
      }
    }

    function closeTab(id) {
      const tab = tabs.find(t => t.id === id);
      if (!tab) return;

      // Cleanup all panes in this tab
      tab.panes.forEach(p => {
        cleanupPane(p);
        p.term.dispose();
      });

      // Remove DOM
      tab.paneContainer.remove();

      // Remove from array
      tabs = tabs.filter(t => t.id !== id);

      // If we closed the active tab, switch to the last tab
      if (activeTabId === id) {
        if (tabs.length > 0) {
          switchTab(tabs[tabs.length - 1].id);
        }
      }

      // If no tabs left, create a new one
      if (tabs.length === 0) {
        tabIdCounter = 0;
        createTab();
        return;
      }

      renderTabBar();
      updateTabButtons();
    }

    function startRenameTab(id) {
      const labelEl = document.querySelector(`#tab-bar .tab[data-tab-id="${id}"] .tab-label`);
      if (!labelEl) return;
      labelEl.contentEditable = 'true';
      labelEl.focus();

      // Select all text
      const range = document.createRange();
      range.selectNodeContents(labelEl);
      window.getSelection().removeAllRanges();
      window.getSelection().addRange(range);

      function finishRename() {
        labelEl.contentEditable = 'false';
        const newLabel = labelEl.textContent.trim();
        const tab = tabs.find(t => t.id === id);
        if (tab && newLabel) {
          tab.label = newLabel;
        } else if (tab) {
          labelEl.textContent = tab.label; // revert empty
        }
        labelEl.removeEventListener('blur', finishRename);
        labelEl.removeEventListener('keydown', handleKey);
        // Refocus terminal
        const ap = getActivePane();
        if (ap) ap.term.focus();
      }

      function handleKey(e) {
        if (e.key === 'Enter') {
          e.preventDefault();
          finishRename();
        }
        if (e.key === 'Escape') {
          e.preventDefault();
          const tab = tabs.find(t => t.id === id);
          if (tab) labelEl.textContent = tab.label;
          finishRename();
        }
      }

      labelEl.addEventListener('blur', finishRename);
      labelEl.addEventListener('keydown', handleKey);
    }
```

**Step 2: Add renderTabBar function**

```javascript
    function renderTabBar() {
      const tabBar = document.getElementById('tab-bar');
      const newTabBtn = document.getElementById('new-tab-btn');

      // Remove old tab elements (keep the + button)
      tabBar.querySelectorAll('.tab').forEach(el => el.remove());

      // Insert tabs before the + button
      tabs.forEach((tab, index) => {
        const tabEl = document.createElement('div');
        tabEl.className = 'tab' + (tab.id === activeTabId ? ' active' : '');
        tabEl.dataset.tabId = tab.id;

        const label = document.createElement('span');
        label.className = 'tab-label';
        label.textContent = tab.label;
        tabEl.appendChild(label);

        const closeBtn = document.createElement('button');
        closeBtn.className = 'tab-close';
        closeBtn.textContent = '\u00D7';
        closeBtn.title = 'Close tab';
        closeBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          closeTab(tab.id);
        });
        tabEl.appendChild(closeBtn);

        // Click to switch
        tabEl.addEventListener('click', () => switchTab(tab.id));

        // Double-click to rename
        tabEl.addEventListener('dblclick', (e) => {
          e.preventDefault();
          startRenameTab(tab.id);
        });

        tabBar.insertBefore(tabEl, newTabBtn);
      });

      // Update + button state
      newTabBtn.disabled = tabs.length >= MAX_TABS;
    }

    function updateTabButtons() {
      // Update toolbar pane buttons for active tab
      const tab = getActiveTab();
      const multi = tab && tab.panes.length > 1;
      document.getElementById('close-pane-btn').style.display = multi ? '' : 'none';
      document.getElementById('next-pane-btn').style.display = multi ? '' : 'none';
      document.getElementById('split-btn').style.display = (tab && tab.panes.length >= 2) ? 'none' : '';
    }
```

**Step 3: Commit**

```bash
git add static/index.html
git commit -m "feat: implement createTab, switchTab, closeTab, renameTab"
```

---

### Task 6: Rewrite splitPane, closeActivePane, cyclePaneFocus for Tab Context

**Files:**
- Modify: `static/index.html` — replace `splitPane`, `closeActivePane`, `cyclePaneFocus` functions

**Step 1: Replace splitPane**

```javascript
    async function splitPane() {
      const tab = getActiveTab();
      if (!tab || tab.panes.length >= 2) return;
      status.textContent = 'Splitting...';
      status.style.display = '';
      try {
        await createPane(tab);
        // Reset flex for even split
        tab.panes.forEach(p => { p.element.style.flex = '1'; });
        refitAllPanes();
        updateTabButtons();
        status.style.display = 'none';
      } catch (e) {
        status.textContent = 'Split failed: ' + e.message;
        status.style.color = '#ff5555';
      }
    }
```

**Step 2: Replace closeActivePane**

```javascript
    function closeActivePane() {
      const tab = getActiveTab();
      if (!tab) return;

      // If only one pane, close the whole tab
      if (tab.panes.length <= 1) {
        closeTab(tab.id);
        return;
      }

      const ap = tab.panes.find(p => p.id === tab.activePaneId) || tab.panes[0];
      if (!ap) return;

      cleanupPane(ap);
      ap.term.dispose();
      ap.element.remove();

      // Remove divider
      if (tab.divider) {
        tab.divider.remove();
        tab.divider = null;
      }

      tab.panes = tab.panes.filter(p => p.id !== ap.id);

      // Reset remaining pane to full width
      if (tab.panes.length === 1) {
        tab.panes[0].element.style.flex = '1';
      }

      focusPane(tab.panes[0].id);
      refitAllPanes();
      updateTabButtons();
    }
```

**Step 3: Replace cyclePaneFocus**

```javascript
    function cyclePaneFocus(direction) {
      const tab = getActiveTab();
      if (!tab || tab.panes.length <= 1) return;
      const idx = tab.panes.findIndex(p => p.id === tab.activePaneId);
      const next = direction === 'next'
        ? (idx + 1) % tab.panes.length
        : (idx - 1 + tab.panes.length) % tab.panes.length;
      focusPane(tab.panes[next].id);
    }
```

**Step 4: Add cycleTabFocus**

```javascript
    function cycleTabFocus(direction) {
      if (tabs.length <= 1) return;
      const idx = tabs.findIndex(t => t.id === activeTabId);
      const next = direction === 'next'
        ? (idx + 1) % tabs.length
        : (idx - 1 + tabs.length) % tabs.length;
      switchTab(tabs[next].id);
    }

    function jumpToTab(number) {
      // number is 1-indexed
      if (number >= 1 && number <= tabs.length) {
        switchTab(tabs[number - 1].id);
      }
    }
```

**Step 5: Commit**

```bash
git add static/index.html
git commit -m "refactor: pane operations now scoped to active tab"
```

---

### Task 7: Update Divider Drag for Per-Tab Dividers

**Files:**
- Modify: `static/index.html` — `setupDividerDrag` function

**Step 1: Update setupDividerDrag to accept tab parameter**

Replace the function:
```javascript
    function setupDividerDrag(divider, tab) {
      let dragging = false;

      divider.addEventListener('mousedown', e => {
        e.preventDefault();
        dragging = true;
        divider.classList.add('dragging');
        document.body.style.cursor = 'col-resize';
        document.body.style.userSelect = 'none';
      });

      document.addEventListener('mousemove', e => {
        if (!dragging || tab.panes.length < 2) return;
        const rect = tab.paneContainer.getBoundingClientRect();
        let pct = ((e.clientX - rect.left) / rect.width) * 100;
        pct = Math.max(15, Math.min(85, pct));
        tab.panes[0].element.style.flex = `0 0 ${pct}%`;
        tab.panes[1].element.style.flex = '1 1 0';
        refitAllPanes();
      });

      document.addEventListener('mouseup', () => {
        if (dragging) {
          dragging = false;
          divider.classList.remove('dragging');
          document.body.style.cursor = '';
          document.body.style.userSelect = '';
          refitAllPanes();
        }
      });
    }
```

**Step 2: Commit**

```bash
git add static/index.html
git commit -m "refactor: divider drag scoped to parent tab"
```

---

### Task 8: Update Keyboard Shortcuts

**Files:**
- Modify: `static/index.html` — the `document.addEventListener('keydown', ...)` block

**Step 1: Replace the shortcut block**

Replace the entire keyboard shortcuts section (lines 633-674) with:
```javascript
    // ── Global Keyboard Shortcuts ──────────────────────────────────
    document.addEventListener('keydown', e => {
      // Ctrl+= : increase font
      if (e.ctrlKey && !e.altKey && !e.shiftKey && (e.key === '=' || e.key === '+')) {
        e.preventDefault(); setFontSize(currentFontSize + 1); return;
      }
      // Ctrl+- : decrease font
      if (e.ctrlKey && !e.altKey && !e.shiftKey && e.key === '-') {
        e.preventDefault(); setFontSize(currentFontSize - 1); return;
      }
      // Ctrl+0 : reset font
      if (e.ctrlKey && !e.altKey && !e.shiftKey && e.key === '0') {
        e.preventDefault(); setFontSize(DEFAULT_FONT_SIZE); return;
      }
      // Ctrl+Shift+F : toggle search
      if (e.ctrlKey && e.shiftKey && e.key === 'F') {
        e.preventDefault(); toggleSearch(); return;
      }
      // Alt+V (Option+V) : toggle voice dictation
      if (e.altKey && !e.ctrlKey && !e.shiftKey && e.code === 'KeyV') {
        e.preventDefault();
        if (dictationActive) closeDictation();
        else startDictation();
        return;
      }

      // ── Tab shortcuts (Ctrl+Shift) ──
      // Ctrl+Shift+T : new tab
      if (e.ctrlKey && e.shiftKey && e.key === 'T') {
        e.preventDefault(); createTab(); return;
      }
      // Ctrl+Shift+W : close active pane (closes tab if last pane)
      if (e.ctrlKey && e.shiftKey && e.key === 'W') {
        e.preventDefault(); closeActivePane(); return;
      }
      // Ctrl+Shift+] : next tab
      if (e.ctrlKey && e.shiftKey && e.code === 'BracketRight') {
        e.preventDefault(); cycleTabFocus('next'); return;
      }
      // Ctrl+Shift+[ : prev tab
      if (e.ctrlKey && e.shiftKey && e.code === 'BracketLeft') {
        e.preventDefault(); cycleTabFocus('prev'); return;
      }
      // Ctrl+Shift+1-5 : jump to tab
      if (e.ctrlKey && e.shiftKey && e.code >= 'Digit1' && e.code <= 'Digit5') {
        e.preventDefault(); jumpToTab(parseInt(e.code.slice(-1))); return;
      }

      // ── Pane shortcuts (Alt+Shift) ──
      // Alt+Shift+D : split pane
      if (e.altKey && e.shiftKey && e.key === 'D') {
        e.preventDefault(); splitPane(); return;
      }
      // Alt+Shift+W : close pane
      if (e.altKey && e.shiftKey && e.key === 'W') {
        e.preventDefault(); closeActivePane(); return;
      }
      // Alt+Shift+] : next pane
      if (e.altKey && e.shiftKey && e.code === 'BracketRight') {
        e.preventDefault(); cyclePaneFocus('next'); return;
      }
      // Alt+Shift+[ : prev pane
      if (e.altKey && e.shiftKey && e.code === 'BracketLeft') {
        e.preventDefault(); cyclePaneFocus('prev'); return;
      }
    });
```

**Step 2: Update toolbar button tooltips**

Update line 224 to reflect new shortcut:
```html
        <button id="split-btn" title="Split pane (Alt+Shift+D)">&#x229E;</button>
        <button id="close-pane-btn" title="Close pane (Alt+Shift+W)" style="display:none;">&#x2715;</button>
        <button id="next-pane-btn" title="Next pane (Alt+Shift+])" style="display:none;">&#x21C6;</button>
```

**Step 3: Commit**

```bash
git add static/index.html
git commit -m "feat: add tab keyboard shortcuts, move pane shortcuts to Alt+Shift"
```

---

### Task 9: Update Toolbar Button Wiring and Cleanup Functions

**Files:**
- Modify: `static/index.html` — toolbar button listeners, cleanupAllPanes, pagehide, updatePaneButtons

**Step 1: Wire the new-tab button**

Add after the existing toolbar button listeners:
```javascript
    document.getElementById('new-tab-btn').addEventListener('click', () => createTab());
```

**Step 2: Update cleanupAllPanes to iterate all tabs**

Replace:
```javascript
    function cleanupAllPanes() {
      panes.forEach(p => cleanupPane(p));
    }
```
With:
```javascript
    function cleanupAllPanes() {
      getAllPanes().forEach(p => cleanupPane(p));
    }
```

**Step 3: Update pagehide beacon to iterate all tabs**

Replace the `pagehide` listener:
```javascript
    window.addEventListener('pagehide', () => {
      getAllPanes().forEach(p => {
        if (p.sessionId) {
          navigator.sendBeacon(
            '/api/heartbeat',
            new Blob([JSON.stringify({ session_id: p.sessionId })], { type: 'application/json' })
          );
        }
      });
    });
```

**Step 4: Replace the old updatePaneButtons function**

The `updatePaneButtons` function was already rewritten in Task 5 as `updateTabButtons`. Remove the old one (lines 930-935) if it still exists.

**Step 5: Commit**

```bash
git add static/index.html
git commit -m "feat: wire new-tab button, update cleanup for tabs"
```

---

### Task 10: Update init() to Create First Tab Instead of First Pane

**Files:**
- Modify: `static/index.html` — `init` function

**Step 1: Replace init**

```javascript
    async function init() {
      try {
        status.textContent = 'Initializing terminal...';

        if (typeof Terminal === 'undefined') throw new Error('xterm.js not loaded');
        if (typeof FitAddon === 'undefined') throw new Error('FitAddon not loaded');

        await createTab();

        status.textContent = 'Connected!';
        setTimeout(() => { status.style.display = 'none'; }, 1000);

        window.addEventListener('resize', () => refitAllPanes());
        window.addEventListener('beforeunload', () => cleanupAllPanes());

      } catch (e) {
        status.textContent = 'Error: ' + e.message;
        status.style.color = '#ff5555';
        console.error(e);
      }
    }
```

**Step 2: Remove the old `<div id="pane-container"></div>`**

This was already replaced in Task 1, but verify it's gone. The `createTab` function now creates per-tab pane containers dynamically.

**Step 3: Verify end-to-end**

Open the page in a browser. Verify:
- Tab bar appears at top with "Shell 1" tab and "+" button
- Terminal renders and works below the tab bar
- Click "+" creates "Shell 2" with its own terminal session
- Clicking tabs switches between them
- Double-click a tab label to rename it
- Click "x" on a tab to close it
- `Ctrl+Shift+T` creates a new tab
- `Ctrl+Shift+[/]` cycles tabs
- `Alt+Shift+D` splits the active tab's pane
- `Alt+Shift+W` closes a pane (or tab if last pane)
- Closing the last tab auto-creates a new "Shell 1"
- Max 5 tabs, "+" button disables at cap

**Step 4: Commit**

```bash
git add static/index.html
git commit -m "feat: init creates first tab, multi-tab terminals complete"
```

---

### Task 11: Remove Dead Code and Final Cleanup

**Files:**
- Modify: `static/index.html`

**Step 1: Remove any remaining references to the old global `panes` variable**

Search for `panes.forEach`, `panes.find`, `panes.length`, `panes.filter`, `panes.push`, `panes.pop`, `panes[` in the file. All should now reference `tab.panes` or `getAllPanes()`. Remove any dead code.

**Step 2: Remove the old `#pane-container` and `#pane-divider` CSS rules if still present**

They've been replaced by `.tab-pane-container` and `.pane-divider`.

**Step 3: Verify no console errors**

Open browser dev tools, check console is clean.

**Step 4: Commit**

```bash
git add static/index.html
git commit -m "chore: remove dead pane code, cleanup"
```
