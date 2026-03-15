const WS_URL = `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws/team`;
const STATE_URL = '/api/scores/team';
const RECONNECT_DELAY = 3000;
const DEFAULT_POLL_SECONDS = 10;
const DEFAULT_STALE_SECONDS = 45;
const DEFAULT_RELOAD_SECONDS = 0;

const params = new URLSearchParams(window.location.search);
const HTTP_POLL_INTERVAL_MS = parseSecondsParam('poll', DEFAULT_POLL_SECONDS) * 1000;
const STALE_RELOAD_INTERVAL_MS = parseSecondsParam('stale', DEFAULT_STALE_SECONDS) * 1000;
const PAGE_RELOAD_INTERVAL_MS = parseSecondsParam('reload', DEFAULT_RELOAD_SECONDS) * 1000;
const PREVIEW_STATE = window.TEAM_PREVIEW_STATE || null;

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
    console.warn(`Reloading team overlay: ${reason}`);
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
            const state = JSON.parse(e.data);
            if (!state.ack) {
                render(state);
            }
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
    if (!state || !Array.isArray(state.teams)) {
        return;
    }

    lastSyncAt = Date.now();
    const el = document.getElementById('scoreboard');
    const teams = state.teams;

    if (!teams.length) {
        el.innerHTML = `<div class="empty-state">No teams are configured yet.</div>`;
        prev = state;
        return;
    }

    const groups = groupTeams(teams);
    el.innerHTML = groups.map(group => {
        const orderedTeams = [...group.teams].sort((left, right) => {
            const leftScore = Number(left.total_score || 0);
            const rightScore = Number(right.total_score || 0);
            if (rightScore !== leftScore) {
                return rightScore - leftScore;
            }
            return String(left.name || '').localeCompare(String(right.name || ''));
        });
        const rows = orderedTeams.map((team, index) => renderTeamRow(team, index + 1)).join('');
        return `<section class="group-cell">
            <table class="group-table">
                <thead>
                    <tr>
                        <th class="team-col">Team</th>
                        <th class="value-col provisional-col">~</th>
                        <th class="value-col final-col">Final</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        </section>`;
    }).join('');

    if (prev) {
        setTimeout(() => {
            document.querySelectorAll('.updated,.score-changed,.pts-changed').forEach((node) => {
                node.classList.remove('updated', 'score-changed', 'pts-changed');
            });
        }, 1500);
    }

    prev = state;
}

function groupTeams(teams) {
    const groups = [];
    const groupsByKey = new Map();

    teams.forEach((team) => {
        const key = team.group || '';
        let group = groupsByKey.get(key);
        if (!group) {
            group = { name: team.group || '', teams: [] };
            groupsByKey.set(key, group);
            groups.push(group);
        }
        group.teams.push(team);
    });

    return groups;
}

function renderTeamRow(team, rank) {
    const prevTeam = prev?.teams?.find((item) => item.id === team.id);
    const currentScore = Number(team.total_score || 0);
    const prevScore = prevTeam ? Number(prevTeam.total_score || 0) : null;
    const provisionalScore = Number(team.provisional_score ?? currentScore);
    const prevProvisionalScore = prevTeam
        ? Number(prevTeam.provisional_score ?? prevTeam.total_score ?? 0)
        : null;
    const showProvisional = team.active_players > 0 && provisionalScore !== currentScore;
    const provisionalChanged = prevProvisionalScore != null && prevProvisionalScore !== provisionalScore;
    const finalChanged = prevScore != null && prevScore !== currentScore;
    const changed = provisionalChanged || finalChanged;
    const scoreText = formatSignedScore(currentScore);
    const scoreClass = valueClass(currentScore);
    const provisionalText = formatSignedScore(provisionalScore);
    const provisionalClass = valueClass(provisionalScore);

    return `<tr class="team-row${changed ? ' updated' : ''}">
        <td class="team-cell">
            <div class="team-wrap">
                <span class="rank rank-${rank}">${rank}</span>
                <span class="team-name">${esc(team.name)}</span>
            </div>
        </td>
        <td class="value-cell provisional-col">
            <div class="value-wrap">
                ${showProvisional ? `<span class="provisional ${provisionalClass}${provisionalChanged ? ' score-changed' : ''}">${provisionalText}</span>` : '<span class="provisional placeholder">&nbsp;</span>'}
            </div>
        </td>
        <td class="value-cell final-col">
            <div class="value-wrap">
                <span class="total ${scoreClass}${finalChanged ? ' score-changed' : ''}">${scoreText}</span>
            </div>
        </td>
    </tr>`;
}

function formatSignedScore(value) {
    const numeric = Number(value || 0);
    return numeric > 0 ? `+${numeric.toFixed(1)}` : numeric.toFixed(1);
}

function valueClass(value) {
    const numeric = Number(value || 0);
    if (numeric > 0) {
        return 'pos';
    }
    if (numeric < 0) {
        return 'neg';
    }
    return 'zero';
}

function esc(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

if (PREVIEW_STATE) {
    render(PREVIEW_STATE);
} else {
    startPolling();
    startStaleWatchdog();
    startPeriodicReload();
    connect();
}
