// ULTRON Tab Reporter — MV3 service worker.
//
// Watches for tab focus/URL changes and POSTs {url, title, active_since_ms}
// to the bridges sidecar's local HTTP receiver.
//
// Why service_worker over a long-running listener page: MV3 forbids
// persistent background pages. The worker spins up on the events we
// register for and shuts down when idle. That's fine — we don't poll;
// we react to tab events.

const ENDPOINT = "http://127.0.0.1:8766/ingest";

// Track when the currently-active tab became active so we can compute
// `active_since_ms` (how long the user has been on this page).
let activeSince = Date.now();
let lastReportedKey = "";

async function reportActiveTab() {
    try {
        const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
        if (!tab || !tab.url) return;

        // Skip internal / extension pages — noise for ULTRON.
        if (
            tab.url.startsWith("chrome://") ||
            tab.url.startsWith("edge://") ||
            tab.url.startsWith("about:") ||
            tab.url.startsWith("chrome-extension://")
        ) {
            return;
        }

        const key = `${tab.url}|${tab.title || ""}`;
        if (key === lastReportedKey) return;
        lastReportedKey = key;
        activeSince = Date.now();

        const body = JSON.stringify({
            url: tab.url,
            title: tab.title || "",
            active_since_ms: 0,
        });

        await fetch(ENDPOINT, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body,
            // Keep the request alive past worker tear-down.
            keepalive: true,
        }).catch(() => { /* ULTRON sidecar offline; that's fine. */ });
    } catch (e) {
        // Swallow — never want a tab handler to throw and stall the worker.
    }
}

chrome.tabs.onActivated.addListener(() => reportActiveTab());
chrome.tabs.onUpdated.addListener((_id, changeInfo) => {
    if (changeInfo.status === "complete" || changeInfo.title || changeInfo.url) {
        reportActiveTab();
    }
});
chrome.windows.onFocusChanged.addListener((winId) => {
    if (winId !== chrome.windows.WINDOW_ID_NONE) reportActiveTab();
});

// Heartbeat: every 30s, re-report the active tab (so the bridge knows
// the user is still on this page if they leave it open). Tab events
// stop firing for long-lived static pages.
chrome.alarms.create("ultron-heartbeat", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === "ultron-heartbeat") reportActiveTab();
});
