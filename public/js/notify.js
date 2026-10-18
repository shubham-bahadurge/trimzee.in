// Trimzee alerts: new bookings for owners, "5 minutes to go" reminders for customers.
// Works while the site is open (tab or home-screen app). Checks every 15 seconds.
(function () {
    if (!window.TZ_AUTH) { return; }
    var box = document.createElement("div");
    box.className = "toasts";
    document.body.appendChild(box);

    function toast(item) {
        var el = document.createElement("div");
        el.className = "toast-item";
        el.textContent = item.message;
        el.addEventListener("click", function () {
            el.remove();
            window.location.href = item.kind === "booking" ? "/dashboard" : "/my-bookings";
        });
        box.appendChild(el);
        setTimeout(function () { el.remove(); }, 15000);
    }
    function system(item) {
        if ("Notification" in window && Notification.permission === "granted") {
            try { new Notification("Trimzee", { body: item.message, icon: "/img/logo-mark.png" }); } catch (e) { /* ignore */ }
        }
    }
    async function poll() {
        if (document.hidden) { return; }      // don't poll while the tab is in the background
        try {
            var res = await fetch("/api/notifications", { credentials: "same-origin" });
            if (!res.ok) { return; }
            var data = await res.json();
            if (!data.items || !data.items.length) { return; }
            data.items.forEach(function (item) { toast(item); system(item); });
            if (navigator.vibrate) { navigator.vibrate([200, 100, 200]); }
            await fetch("/api/notifications/read", {
                method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ ids: data.items.map(function (i) { return i.id; }) })
            });
            if (window.TZ_ROLE === "owner" && location.pathname === "/dashboard") {
                setTimeout(function () { location.reload(); }, 2500);
            }
        } catch (e) { /* offline: try again next time */ }
    }
    var enable = document.getElementById("enableAlerts");
    if (enable) {
        enable.addEventListener("click", function () {
            if (!("Notification" in window)) { enable.textContent = "Alerts not supported here"; return; }
            Notification.requestPermission().then(function (p) {
                enable.textContent = p === "granted" ? "🔔 Alerts are on" : "🔕 Alerts blocked in browser";
            });
        });
        if ("Notification" in window && Notification.permission === "granted") { enable.textContent = "🔔 Alerts are on"; }
    }
    poll();
    setInterval(poll, 15000);
    document.addEventListener("visibilitychange", function () { if (!document.hidden) { poll(); } });
})();
