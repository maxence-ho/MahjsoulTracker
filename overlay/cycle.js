const params = new URLSearchParams(window.location.search);
const DEFAULT_INTERVAL_SECONDS = 15;
const DEFAULT_START_VIEW = 'players';

const playerFrame = document.getElementById('player-frame');
const teamFrame = document.getElementById('team-frame');
const playerShell = playerFrame.parentElement;
const teamShell = teamFrame.parentElement;
const DEFAULT_PLAYER_SRC = window.CYCLE_PLAYER_SRC || './';
const DEFAULT_TEAM_SRC = window.CYCLE_TEAM_SRC || './team.html';
const frames = {
    players: { frame: playerFrame, shell: playerShell },
    teams: { frame: teamFrame, shell: teamShell },
};

let activeView = params.get('start') === 'team' ? 'teams' : DEFAULT_START_VIEW;

function parseIntervalSeconds() {
    const raw = params.get('interval');
    if (raw == null || raw === '') {
        return DEFAULT_INTERVAL_SECONDS;
    }

    const parsed = Number(raw);
    if (!Number.isFinite(parsed) || parsed <= 0) {
        return DEFAULT_INTERVAL_SECONDS;
    }

    return parsed;
}

function childQueryString() {
    const childParams = new URLSearchParams(params);
    childParams.delete('interval');
    childParams.delete('start');
    childParams.delete('player_src');
    childParams.delete('team_src');
    const serialized = childParams.toString();
    return serialized ? `?${serialized}` : '';
}

function frameSrc(name) {
    const configured = params.get(`${name}_src`);
    return configured || (name === 'player' ? DEFAULT_PLAYER_SRC : DEFAULT_TEAM_SRC);
}

function appendQuery(url, query) {
    if (!query) {
        return url;
    }
    return `${url}${url.includes('?') ? '&' : ''}${query.slice(1)}`;
}

function activate(viewName) {
    Object.entries(frames).forEach(([name, entry]) => {
        entry.shell.classList.toggle('active', name === viewName);
    });
    activeView = viewName;
    refreshFrameFit(viewName);
}

function toggleView() {
    activate(activeView === 'players' ? 'teams' : 'players');
}

function initializeFrames() {
    const query = childQueryString();
    playerFrame.src = appendQuery(frameSrc('player'), query);
    teamFrame.src = appendQuery(frameSrc('team'), query);

    Object.keys(frames).forEach((viewName) => {
        const { frame } = frames[viewName];
        frame.addEventListener('load', () => {
            refreshFrameFit(viewName);
            window.setTimeout(() => refreshFrameFit(viewName), 150);
            window.setTimeout(() => refreshFrameFit(viewName), 600);
        });
    });

    activate(activeView);
}

function childDocumentSize(frame) {
    const doc = frame.contentDocument;
    if (!doc) {
        return null;
    }

    const body = doc.body;
    const root = doc.documentElement;
    if (!body || !root) {
        return null;
    }

    const width = Math.max(
        body.scrollWidth,
        body.offsetWidth,
        root.scrollWidth,
        root.offsetWidth,
        root.clientWidth,
    );
    const height = Math.max(
        body.scrollHeight,
        body.offsetHeight,
        root.scrollHeight,
        root.offsetHeight,
        root.clientHeight,
    );

    if (!width || !height) {
        return null;
    }

    return { width, height };
}

function refreshFrameFit(viewName) {
    const entry = frames[viewName];
    if (!entry) {
        return;
    }

    const { frame, shell } = entry;
    try {
        const size = childDocumentSize(frame);
        if (!size) {
            frame.style.setProperty('--fit-scale', '1');
            frame.style.setProperty('--frame-width', '100%');
            frame.style.setProperty('--frame-height', '100%');
            return;
        }

        const widthScale = shell.clientWidth > 0 ? shell.clientWidth / size.width : 1;
        const heightScale = shell.clientHeight > 0 ? shell.clientHeight / size.height : 1;
        const fitScale = Math.min(1, widthScale, heightScale);
        const safeScale = Number.isFinite(fitScale) && fitScale > 0 ? fitScale : 1;

        frame.style.setProperty('--fit-scale', safeScale.toString());
        frame.style.setProperty('--frame-width', `${100 / safeScale}%`);
        frame.style.setProperty('--frame-height', `${100 / safeScale}%`);
    } catch (_error) {
        frame.style.setProperty('--fit-scale', '1');
        frame.style.setProperty('--frame-width', '100%');
        frame.style.setProperty('--frame-height', '100%');
    }
}

function refreshAllFrameFits() {
    Object.keys(frames).forEach(refreshFrameFit);
}

initializeFrames();
refreshAllFrameFits();
window.addEventListener('resize', refreshAllFrameFits);
window.setInterval(refreshAllFrameFits, 1000);
window.setInterval(toggleView, parseIntervalSeconds() * 1000);
