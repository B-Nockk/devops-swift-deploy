// metrics.js — SwiftDeploy Dashboard
//
// Polls the BFF snapshot endpoint and updates the DOM.
// No build step required — plain JS, no dependencies.
//
// ── To change behaviour ───────────────────────────────────────────────────
// Edit CONFIG below. Nothing else needs to change for common tweaks.
//
// ── To add a new metric ───────────────────────────────────────────────────
// 1. Add a field to DashboardSnapshot in Go (ports.go + service.go)
// 2. Add an element to index.html with an id
// 3. Add one line to render() below referencing that id
// ─────────────────────────────────────────────────────────────────────────

const CONFIG = {
  // URL of the BFF snapshot endpoint.
  // The browser fetches this — not /metrics directly.
  snapshotUrl: '/api/dashboard/snapshot',

  // How often to poll in milliseconds.
  // Lower = more responsive but more requests.
  pollIntervalMs: 5000,

  // Error rate threshold (percentage) above which the card turns red.
  errorRateWarnPct: 1.0,
  errorRateDangerPct: 5.0,

  // P99 latency thresholds in milliseconds.
  p99WarnMs: 250,
  p99DangerMs: 500,
};

// ── DOM refs — update these if you rename element ids in index.html ────────

const $ = id => document.getElementById(id);

const els = {
  modeBadge:     $('mode-badge'),
  chaosBadge:    $('chaos-badge'),
  version:       $('version'),
  uptime:        $('uptime'),
  statusDot:     $('status-dot'),
  totalRequests: $('total-requests'),
  errorRate:     $('error-rate'),
  p99Latency:    $('p99-latency'),
  errorRequests: $('error-requests'),
  routesTbody:   $('routes-tbody'),
  lastUpdated:   $('last-updated'),
};

// ── State ─────────────────────────────────────────────────────────────────

let consecutiveErrors = 0;

// ── Main poll loop ────────────────────────────────────────────────────────

async function poll() {
  try {
    const res = await fetch(CONFIG.snapshotUrl);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    render(data);
    consecutiveErrors = 0;
    setStatus('ok');
  } catch (err) {
    consecutiveErrors++;
    setStatus('error');
    console.warn('Dashboard fetch failed:', err.message);
  }
}

// ── Render — one function, one place for all DOM updates ─────────────────
//
// data is a DashboardSnapshot object. Field names match ports.go exactly.
// To add a new field: add one line here.

function render(data) {
  // Header
  setModeBadge(data.mode);
  setChaosBadge(data.chaos_active_text);
  els.version.textContent = data.version;
  els.uptime.textContent  = data.uptime_human;

  // Cards
  els.totalRequests.textContent = fmt.number(data.total_requests);
  els.errorRequests.textContent = fmt.number(data.error_requests);

  const errorPct = data.error_rate_pct;
  els.errorRate.textContent = fmt.pct(errorPct);
  els.errorRate.className = 'card-value ' + thresholdClass(
    errorPct, CONFIG.errorRateWarnPct, CONFIG.errorRateDangerPct
  );

  const p99 = data.p99_latency_ms;
  els.p99Latency.textContent = fmt.ms(p99);
  els.p99Latency.className = 'card-value ' + thresholdClass(
    p99, CONFIG.p99WarnMs, CONFIG.p99DangerMs
  );

  // Route table
  renderRoutes(data.routes || []);

  // Footer
  els.lastUpdated.textContent = 'Last updated: ' + new Date().toLocaleTimeString();
}

function renderRoutes(routes) {
  if (routes.length === 0) {
    els.routesTbody.innerHTML = '<tr><td colspan="4" class="empty">No requests recorded yet.</td></tr>';
    return;
  }

  els.routesTbody.innerHTML = routes.map(r => `
    <tr>
      <td>${esc(r.method)}</td>
      <td>${esc(r.path)}</td>
      <td class="${statusClass(r.status_code)}">${esc(r.status_code)}</td>
      <td>${fmt.number(r.count)}</td>
    </tr>
  `).join('');
}

// ── Helpers ───────────────────────────────────────────────────────────────

function setModeBadge(mode) {
  els.modeBadge.textContent = mode;
  els.modeBadge.className = 'badge badge-' + mode;
}

function setChaosBadge(chaosText) {
  if (chaosText === 'none') {
    els.chaosBadge.classList.add('hidden');
  } else {
    els.chaosBadge.classList.remove('hidden');
    els.chaosBadge.textContent = '⚡ chaos: ' + chaosText;
    els.chaosBadge.className = 'badge badge-chaos';
  }
}

function setStatus(state) {
  els.statusDot.className = 'dot dot-' + state;
  els.statusDot.title = state === 'ok' ? 'Connected' : 'Connection error';
}

// Returns 'ok', 'warning', or 'danger' based on value vs thresholds.
function thresholdClass(value, warn, danger) {
  if (value >= danger) return 'danger';
  if (value >= warn)   return 'warning';
  return 'ok';
}

// Returns a CSS class for coloring status codes.
function statusClass(code) {
  if (code.startsWith('2')) return 'status-2xx';
  if (code.startsWith('4')) return 'status-4xx';
  if (code.startsWith('5')) return 'status-5xx';
  return '';
}

// Escape HTML to prevent XSS from metric label values.
function esc(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

// Formatters — change display format here without touching render().
const fmt = {
  number: n  => Number(n).toLocaleString(),
  pct:    n  => Number(n).toFixed(2) + '%',
  ms:     ms => ms < 1 ? '<1ms' : Number(ms).toFixed(1) + 'ms',
};

// ── Start ─────────────────────────────────────────────────────────────────

poll(); // immediate first fetch
setInterval(poll, CONFIG.pollIntervalMs);
