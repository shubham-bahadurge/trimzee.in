// Trimzee Shop "coming soon" button
(function () {
    var btn = document.getElementById("shopBtn"), panel = document.getElementById("shopPanel");
    if (!btn) { return; }
    btn.addEventListener("click", function (e) {
        e.stopPropagation();
        panel.hidden = !panel.hidden;
        btn.setAttribute("aria-expanded", String(!panel.hidden));
    });
    panel.addEventListener("click", function (e) { e.stopPropagation(); });
    document.addEventListener("click", function () { panel.hidden = true; btn.setAttribute("aria-expanded", "false"); });
    var notify = document.getElementById("shopNotify");
    if (notify) {
        notify.addEventListener("click", async function () {
            notify.disabled = true;
            try {
                var res = await fetch("/api/shop-waitlist", { method: "POST", credentials: "same-origin" });
                notify.textContent = res.ok ? "✓ You're on the list" : "Please try again";
                if (!res.ok) { notify.disabled = false; }
            } catch (e) { notify.textContent = "Please try again"; notify.disabled = false; }
        });
    }
})();
