// Trimzee Shop waitlist interaction
(function () {
    var notify = document.getElementById("shopNotify");
    var status = document.getElementById("shopStatus");
    if (!notify) { return; }
    notify.addEventListener("click", async function () {
        notify.disabled = true;
        status.textContent = "";
        try {
            var res = await fetch("/api/shop-waitlist", { method: "POST", credentials: "same-origin" });
            var data = await res.json();
            if (!res.ok) { throw new Error(data.error || "Please try again."); }
            notify.textContent = "✓ You're on the list";
        } catch (error) {
            status.textContent = error.message || "Could not join the list. Please try again.";
            notify.disabled = false;
        }
    });
})();
