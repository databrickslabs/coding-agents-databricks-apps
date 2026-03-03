/**
 * poll-worker.js — Web Worker for terminal output polling and heartbeat.
 *
 * Runs in a Web Worker so it is NOT throttled by the browser when the tab
 * is in the background.  Manages per-pane polling state, retry/backoff,
 * and foreground/background mode switching.
 *
 * Message protocol (main → worker):
 *   { type: 'start_poll',        paneId, sessionId }
 *   { type: 'stop_poll',         paneId }
 *   { type: 'visibility_change', hidden: bool }
 *
 * Message protocol (worker → main):
 *   { type: 'output',            paneId, data }
 *   { type: 'session_ended',     paneId, reason }  — 'exited' | 'auth_expired' | 'shutting_down'
 *   { type: 'connection_status', paneId, status, attempt, maxAttempts }
 *   { type: 'session_dead',      paneId }
 */

/* eslint-env worker */
"use strict";

// ── Constants ─────────────────────────────────────────────────────────────
const POLL_INTERVAL_FG = 100;     // ms — foreground output poll
const HEARTBEAT_INTERVAL_BG = 30000; // ms — background heartbeat
const RETRY_BASE_MS = 500;
const RETRY_MULTIPLIER = 2;
const RETRY_MAX_DELAY_MS = 10000;
const RETRY_MAX_ATTEMPTS = 5;

// ── Per-pane state ────────────────────────────────────────────────────────
const panes = new Map();
// Each entry: { sessionId, pollTimerId, heartbeatTimerId, retryCount, mode }

let globalHidden = false;  // current tab visibility

// ── Retry helpers ─────────────────────────────────────────────────────────

function retryDelay(attempt) {
  const base = RETRY_BASE_MS * Math.pow(RETRY_MULTIPLIER, attempt);
  const capped = Math.min(base, RETRY_MAX_DELAY_MS);
  // Add jitter: 0.5x–1.5x
  return capped * (0.5 + Math.random());
}

// ── Polling logic ─────────────────────────────────────────────────────────

async function pollOutput(paneId) {
  const state = panes.get(paneId);
  if (!state) return;

  try {
    const resp = await fetch("/api/output", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: state.sessionId }),
    });

    if (!resp.ok) {
      if (resp.status === 403) {
        self.postMessage({ type: "session_ended", paneId, reason: "auth_expired" });
        stopPane(paneId);
        return;
      }
      // 404 or 5xx — retryable
      throw new Error(`HTTP ${resp.status}`);
    }

    // Success — reset retry counter
    state.retryCount = 0;

    const data = await resp.json();

    if (data.shutting_down) {
      self.postMessage({ type: "session_ended", paneId, reason: "shutting_down" });
      stopPane(paneId);
      return;
    }

    // Forward output + flags to main thread
    self.postMessage({ type: "output", paneId, data });

    if (data.exited) {
      self.postMessage({ type: "session_ended", paneId, reason: "exited" });
      stopPane(paneId);
    }
  } catch (err) {
    handleRetry(paneId, err);
  }
}

async function sendHeartbeat(paneId) {
  const state = panes.get(paneId);
  if (!state) return;

  try {
    const resp = await fetch("/api/heartbeat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: state.sessionId }),
    });

    if (!resp.ok) {
      if (resp.status === 403) {
        self.postMessage({ type: "session_ended", paneId, reason: "auth_expired" });
        stopPane(paneId);
        return;
      }
      throw new Error(`HTTP ${resp.status}`);
    }

    // Success — reset retry counter
    state.retryCount = 0;

    const data = await resp.json();
    if (data.timeout_warning) {
      self.postMessage({
        type: "output",
        paneId,
        data: { timeout_warning: true, output: "", exited: false, shutting_down: false },
      });
    }
  } catch (err) {
    handleRetry(paneId, err);
  }
}

// ── Retry / backoff ───────────────────────────────────────────────────────

function handleRetry(paneId, err) {
  const state = panes.get(paneId);
  if (!state) return;

  state.retryCount++;

  if (state.retryCount > RETRY_MAX_ATTEMPTS) {
    self.postMessage({ type: "session_dead", paneId });
    stopPane(paneId);
    return;
  }

  // Notify main thread of reconnection attempt
  self.postMessage({
    type: "connection_status",
    paneId,
    status: "reconnecting",
    attempt: state.retryCount,
    maxAttempts: RETRY_MAX_ATTEMPTS,
  });

  // Stop current timers and schedule retry
  clearTimers(state);
  const delay = retryDelay(state.retryCount - 1);
  state.pollTimerId = setTimeout(() => {
    if (!panes.has(paneId)) return;
    // Re-notify connected on success (handled in poll/heartbeat success path)
    self.postMessage({
      type: "connection_status",
      paneId,
      status: "connected",
      attempt: 0,
      maxAttempts: RETRY_MAX_ATTEMPTS,
    });
    applyMode(paneId);
  }, delay);
}

// ── Mode management ───────────────────────────────────────────────────────

function clearTimers(state) {
  if (state.pollTimerId) {
    clearInterval(state.pollTimerId);
    clearTimeout(state.pollTimerId);
    state.pollTimerId = null;
  }
  if (state.heartbeatTimerId) {
    clearInterval(state.heartbeatTimerId);
    clearTimeout(state.heartbeatTimerId);
    state.heartbeatTimerId = null;
  }
}

function applyMode(paneId) {
  const state = panes.get(paneId);
  if (!state) return;

  clearTimers(state);
  state.mode = globalHidden ? "background" : "foreground";

  if (state.mode === "foreground") {
    // Poll output at 100ms
    pollOutput(paneId); // immediate first poll
    state.pollTimerId = setInterval(() => pollOutput(paneId), POLL_INTERVAL_FG);
  } else {
    // Background: heartbeat only at 30s
    sendHeartbeat(paneId); // immediate first heartbeat
    state.heartbeatTimerId = setInterval(() => sendHeartbeat(paneId), HEARTBEAT_INTERVAL_BG);
  }
}

// ── Pane lifecycle ────────────────────────────────────────────────────────

function startPane(paneId, sessionId) {
  // Stop existing if any
  stopPane(paneId);

  const state = {
    sessionId,
    pollTimerId: null,
    heartbeatTimerId: null,
    retryCount: 0,
    mode: globalHidden ? "background" : "foreground",
  };
  panes.set(paneId, state);
  applyMode(paneId);
}

function stopPane(paneId) {
  const state = panes.get(paneId);
  if (!state) return;
  clearTimers(state);
  panes.delete(paneId);
}

// ── Message handler ───────────────────────────────────────────────────────

self.onmessage = function (event) {
  const msg = event.data;

  switch (msg.type) {
    case "start_poll":
      startPane(msg.paneId, msg.sessionId);
      break;

    case "stop_poll":
      stopPane(msg.paneId);
      break;

    case "visibility_change":
      globalHidden = msg.hidden;
      // Switch all panes to new mode
      for (const paneId of panes.keys()) {
        applyMode(paneId);
      }
      break;
  }
};
