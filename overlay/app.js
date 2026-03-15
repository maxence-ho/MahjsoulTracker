const WS_URL = `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws`;
const STATE_URL = '/api/scores';
const RECONNECT_DELAY = 3000;
const DEFAULT_POLL_SECONDS = 10;
const DEFAULT_STALE_SECONDS = 45;
const DEFAULT_RELOAD_SECONDS = 0;

const params = new URLSearchParams(window.location.search);
const HTTP_POLL_INTERVAL_MS = parseSecondsParam('poll', DEFAULT_POLL_SECONDS) * 1000;
const STALE_RELOAD_INTERVAL_MS = parseSecondsParam('stale', DEFAULT_STALE_SECONDS) * 1000;
const PAGE_RELOAD_INTERVAL_MS = parseSecondsParam('reload', DEFAULT_RELOAD_SECONDS) * 1000;
const PREVIEW_STATE = window.PLAYER_PREVIEW_STATE || null;

let ws = null;
let reconnectTimer = null;
let fetchInFlight = false;
let lastSyncAt = Date.now();
let prev = null;

function parseSecondsParam(name, fallbackSeconds) {
    const raw = params.get(name);
    if (raw == null || raw === '') {
        return fallbackSeconds;
    }

    const parsed = Number(raw);
    if (!Number.isFinite(parsed) || parsed < 0) {
        return fallbackSeconds;
    }

    return parsed;
}

function scheduleReconnect() {
    if (reconnectTimer) {
        clearTimeout(reconnectTimer);
    }
    reconnectTimer = setTimeout(connect, RECONNECT_DELAY);
}

function reloadPage(reason) {
    console.warn(`Reloading overlay: ${reason}`);
    const url = new URL(window.location.href);
    url.searchParams.set('_ts', Date.now().toString());
    window.location.replace(url.toString());
}

function connect() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
        return;
    }

    ws = new WebSocket(WS_URL);
    ws.onopen = () => {
        console.log('Connected');
        requestRefresh();
    };
    ws.onmessage = (e) => {
        try {
            const s = JSON.parse(e.data);
            if (!s.ack) render(s);
        } catch (err) {
            console.error(err);
        }
    };
    ws.onclose = () => {
        ws = null;
        scheduleReconnect();
    };
    ws.onerror = () => ws.close();
}

function requestRefresh() {
    if (ws?.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ command: 'refresh' }));
    }
}

async function fetchState(reason = 'poll') {
    if (fetchInFlight) {
        return;
    }

    fetchInFlight = true;
    try {
        const response = await fetch(STATE_URL, {
            cache: 'no-store',
            headers: { 'Cache-Control': 'no-cache' },
        });
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        const state = await response.json();
        if (!state.error) {
            render(state);
        }
    } catch (err) {
        console.warn(`State fetch failed (${reason})`, err);
    } finally {
        fetchInFlight = false;
    }
}

function startPolling() {
    fetchState('initial');

    if (HTTP_POLL_INTERVAL_MS > 0) {
        window.setInterval(() => {
            fetchState('poll');
        }, HTTP_POLL_INTERVAL_MS);
    }
}

function startStaleWatchdog() {
    if (STALE_RELOAD_INTERVAL_MS <= 0) {
        return;
    }

    const checkInterval = Math.min(10000, STALE_RELOAD_INTERVAL_MS);
    window.setInterval(() => {
        if (Date.now() - lastSyncAt >= STALE_RELOAD_INTERVAL_MS) {
            reloadPage(`stale for ${Math.round(STALE_RELOAD_INTERVAL_MS / 1000)}s`);
        }
    }, checkInterval);
}

function startPeriodicReload() {
    if (PAGE_RELOAD_INTERVAL_MS <= 0) {
        return;
    }

    window.setInterval(() => {
        reloadPage(`scheduled every ${Math.round(PAGE_RELOAD_INTERVAL_MS / 1000)}s`);
    }, PAGE_RELOAD_INTERVAL_MS);
}

function render(state) {
    if (!state || !Array.isArray(state.players)) {
        return;
    }

    lastSyncAt = Date.now();
    const el = document.getElementById('scoreboard');
    const total = state.games_count || 4;

    el.innerHTML = state.players.map((p, i) => {
        const rank = i + 1;
        const prevP = prev?.players?.find(x => x.account_id === p.account_id);
        const changed = prevP && prevP.total_score !== p.total_score;
        const displayName = p.display_name || p.nickname || p.name;
        const isInGame = p.status === 'in_game';
        const hasLivePoints = isInGame && p.current_points != null;

        let dots = '';
        for (let j = 0; j < total; j++) {
            const isCurrentGame = isInGame && j === p.games.length;
            if (j < p.games.length) {
                dots += `<span class="dot p${p.games[j].placement}">${p.games[j].placement}</span>`;
            } else {
                dots += `<span class="dot empty${isCurrentGame ? ' current-game' : ''}"></span>`;
            }
        }

        const t = p.total_score;
        const ts = t > 0 ? `+${t.toFixed(1)}` : t.toFixed(1);
        const tc = t > 0 ? 'pos' : t < 0 ? 'neg' : 'zero';
        const labels = { idle: 'wait', in_game: 'live', finished: 'done' };
        const roundLabel = isInGame && p.current_round ? ` ${p.current_round}` : '';

        const prevPts = prevP?.current_points;
        const ptsChanged = hasLivePoints && prevPts != null && prevPts !== p.current_points;
        let scoreHtml = `<span class="total ${tc}${changed ? ' score-changed' : ''}">${ts}</span>`;
        if (hasLivePoints) {
            const pts = p.current_points;
            const fmt = pts.toLocaleString();
            scoreHtml = `<span class="score-stack">
                <span class="live-pts primary${ptsChanged ? ' pts-changed' : ''}">${fmt}</span>
                <span class="total subtle ${tc}${changed ? ' score-changed' : ''}">${ts}</span>
            </span>`;
        }

        return `<div class="card${changed ? ' updated' : ''}">
            <div class="card-top">
                <span class="rank rank-${rank}">${rank}</span>
                <span class="name">${esc(displayName)}</span>
                ${scoreHtml}
            </div>
            <div class="card-bottom">
                <span class="status status-${p.status}">${labels[p.status] || p.status}${roundLabel}</span>
                <span class="dots">${dots}</span>
            </div>
        </div>`;
    }).join('');

    if (prev) setTimeout(() => {
        document.querySelectorAll('.updated,.score-changed,.pts-changed').forEach(e => e.classList.remove('updated','score-changed','pts-changed'));
    }, 1500);
    prev = state;
}

function esc(t) { const d = document.createElement('div'); d.textContent = t; return d.innerHTML; }

if (PREVIEW_STATE) {
    render(PREVIEW_STATE);
} else {
    startPolling();
    startStaleWatchdog();
    startPeriodicReload();
    connect();
}
