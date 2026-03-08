/**
 * poll-worker.js — Web Worker for terminal output polling and heartbeat.
 *
 * Runs in a Web Worker so it is NOT throttled by the browser when the tab
 * is in the background. Uses batch polling to fetch output for all panes
 * in a single HTTP request.
 *
 * Message protocol (main → worker):
 *   { type: 'start_poll',        paneId, sessionId }
 *   { type: 'stop_poll',         paneId }
 *   { type: 'visibility_change', hidden: bool }
 *
 * Message protocol (worker → main):
 *   { type: 'output',            paneId, data }
 *   { type: 'session_ended',     paneId, reason }
 *   { type: 'connection_status', paneId, status, attempt, maxAttempts }
 *   { type: 'session_dead',      paneId }
 */

/* eslint-env worker */
"use strict";

// ── Constants ─────────────────────────────────────────────────────────────
const POLL_INTERVAL_FG = 100;        // ms — foreground batch poll
const HEARTBEAT_INTERVAL_BG = 30000; // ms — background heartbeat
const RETRY_BASE_MS = 500;
const RETRY_MULTIPLIER = 2;
const RETRY_MAX_DELAY_MS = 10000;
const RETRY_MAX_ATTEMPTS = 5;

// ── Per-pane state ────────────────────────────────────────────────────────
const panes = new Map();
// Each entry: { sessionId }

let globalHidden = false;
let batchTimerId = null;
let retryCount = 0;

// ── Retry helpers ─────────────────────────────────────────────────────────

function retryDelay(attempt) {
  const base = RETRY_BASE_MS * Math.pow(RETRY_MULTIPLIER, attempt);
  const capped = Math.min(base, RETRY_MAX_DELAY_MS);
  return capped * (0.5 + Math.random());
}

// ── Batch polling logic ──────────────────────────────────────────────────

async function batchPoll() {
  if (panes.size === 0) return;

  const sessionIds = [];
  const sidToPaneId = new Map();
  for (const [paneId, state] of panes) {
    sessionIds.push(state.sessionId);
    sidToPaneId.set(state.sessionId, paneId);
  }

  try {
    const resp = await fetch("/api/output-batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_ids: sessionIds }),
    });

    if (!resp.ok) {
      if (resp.status === 403) {
        for (const paneId of panes.keys()) {
          self.postMessage({ type: "session_ended", paneId, reason: "auth_expired" });
        }
        stopAllPanes();
        return;
      }
      throw new Error(`HTTP ${resp.status}`);
    }

    retryCount = 0;
    const result = await resp.json();

    if (result.shutting_down) {
      for (const paneId of panes.keys()) {
        self.postMessage({ type: "session_ended", paneId, reason: "shutting_down" });
      }
      // Don't stopAllPanes() — retry with backoff so we
      // auto-recover when the new server comes up.
      handleRetry(new Error("Server shutting down"));
      return;
    }

    // Distribute outputs to each pane
    for (const [sid, data] of Object.entries(result.outputs || {})) {
      const paneId = sidToPaneId.get(sid);
      if (!paneId) continue;

      self.postMessage({ type: "output", paneId, data });

      if (data.exited) {
        self.postMessage({ type: "session_ended", paneId, reason: "exited" });
        panes.delete(paneId);
      }
    }
  } catch (err) {
    handleRetry(err);
  }
}

async function batchHeartbeat() {
  if (panes.size === 0) return;

  const sessionIds = [];
  for (const state of panes.values()) {
    sessionIds.push(state.sessionId);
  }

  try {
    const resp = await fetch("/api/output-batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_ids: sessionIds }),
    });

    if (!resp.ok) {
      if (resp.status === 403) {
        for (const paneId of panes.keys()) {
          self.postMessage({ type: "session_ended", paneId, reason: "auth_expired" });
        }
        stopAllPanes();
        return;
      }
      throw new Error(`HTTP ${resp.status}`);
    }

    retryCount = 0;

    const result = await resp.json();
    for (const [sid, data] of Object.entries(result.outputs || {})) {
      if (data.timeout_warning) {
        for (const [paneId, state] of panes) {
          if (state.sessionId === sid) {
            self.postMessage({
              type: "output", paneId,
              data: { timeout_warning: true, output: "", exited: false, shutting_down: false },
            });
          }
        }
      }
    }
  } catch (err) {
    handleRetry(err);
  }
}

// ── Retry / backoff ───────────────────────────────────────────────────────

function handleRetry(err) {
  retryCount++;

  if (retryCount > RETRY_MAX_ATTEMPTS) {
    for (const paneId of panes.keys()) {
      self.postMessage({ type: "session_dead", paneId });
    }
    stopAllPanes();
    return;
  }

  for (const paneId of panes.keys()) {
    self.postMessage({
      type: "connection_status", paneId,
      status: "reconnecting",
      attempt: retryCount, maxAttempts: RETRY_MAX_ATTEMPTS,
    });
  }

  clearBatchTimer();
  const delay = retryDelay(retryCount - 1);
  batchTimerId = setTimeout(() => {
    for (const paneId of panes.keys()) {
      self.postMessage({
        type: "connection_status", paneId,
        status: "connected", attempt: 0, maxAttempts: RETRY_MAX_ATTEMPTS,
      });
    }
    startBatchTimer();
  }, delay);
}

// ── Timer management ──────────────────────────────────────────────────────

function clearBatchTimer() {
  if (batchTimerId) {
    clearInterval(batchTimerId);
    clearTimeout(batchTimerId);
    batchTimerId = null;
  }
}

function startBatchTimer() {
  clearBatchTimer();
  if (panes.size === 0) return;

  if (globalHidden) {
    batchHeartbeat();
    batchTimerId = setInterval(() => batchHeartbeat(), HEARTBEAT_INTERVAL_BG);
  } else {
    batchPoll();
    batchTimerId = setInterval(() => batchPoll(), POLL_INTERVAL_FG);
  }
}

function stopAllPanes() {
  clearBatchTimer();
  panes.clear();
}

// ── Message handler ───────────────────────────────────────────────────────

self.onmessage = function (event) {
  const msg = event.data;

  switch (msg.type) {
    case "start_poll":
      panes.set(msg.paneId, { sessionId: msg.sessionId });
      startBatchTimer();
      break;

    case "stop_poll":
      panes.delete(msg.paneId);
      if (panes.size === 0) clearBatchTimer();
      break;

    case "visibility_change":
      globalHidden = msg.hidden;
      startBatchTimer();
      break;
  }
};
