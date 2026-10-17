// Trimzee salon page: live slots, 3-minute slot reservation, 5 km live-location check, booking + confirmation code
const MAX_KM = 5;
const POLL_MS = 5000;
const info = document.body.dataset;
const salonId = info.salonId;
const auth = info.auth;
const salonLat = parseFloat(info.lat);
const salonLng = parseFloat(info.lng);
const grid = document.getElementById("slots");
const confirmBtn = document.getElementById("confirm");
const message = document.getElementById("message");
const closedMsg = document.getElementById("closedMsg");
const rangeInfo = document.getElementById("rangeInfo");
const holdInfo = document.getElementById("holdInfo");
const tabs = Array.from(document.querySelectorAll(".day-tab"));
const modal = document.getElementById("modal");

let day = tabs.length ? tabs[0].dataset.day : "";
let selected = null;
let holdUntil = 0;
let holdTimer = null;
let here = null;
let nearOk = false;
let lastData = null;

function haversineKm(lat1, lon1, lat2, lon2) {
    const rad = Math.PI / 180;
    const dLat = (lat2 - lat1) * rad;
    const dLon = (lon2 - lon1) * rad;
    const a = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
        Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(dLon / 2) * Math.sin(dLon / 2);
    return 6371 * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

function pretty(t) {
    const parts = t.split(":");
    const h = parseInt(parts[0], 10);
    return ((h + 11) % 12 + 1) + ":" + parts[1] + " " + (h >= 12 ? "PM" : "AM");
}

function showMessage(text, ok) {
    message.textContent = text;
    message.className = "message " + (ok ? "ok" : "err");
    message.hidden = false;
}

function post(url, body) {
    return fetch(url, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}) });
}

function updateButton() {
    if (auth !== "customer") {
        confirmBtn.disabled = true;
        confirmBtn.textContent = auth === "" ? "Sign in to book" : "Customer account needed";
    } else if (!nearOk) {
        confirmBtn.disabled = true;
        confirmBtn.textContent = "Turn on location to book";
    } else if (!selected) {
        confirmBtn.disabled = true;
        confirmBtn.textContent = "Select a time to continue";
    } else {
        confirmBtn.disabled = false;
        confirmBtn.textContent = "Confirm booking for " + pretty(selected.slice(11));
    }
}

function updateRange() {
    rangeInfo.className = "range-info";
    if (!here) {
        nearOk = false;
        rangeInfo.textContent = "📍 Allow location to book. Salons must be within 5 km of you.";
    } else if (isNaN(salonLat) || isNaN(salonLng)) {
        nearOk = false;
        rangeInfo.classList.add("bad");
        rangeInfo.textContent = "This salon hasn't shared its location yet, so booking isn't available.";
    } else {
        const km = haversineKm(here.latitude, here.longitude, salonLat, salonLng);
        nearOk = km <= MAX_KM;
        rangeInfo.classList.add(nearOk ? "good" : "bad");
        rangeInfo.textContent = nearOk ? "📍 You're " + km.toFixed(1) + " km away. You can book this salon." :
            "📍 You're " + km.toFixed(1) + " km away. You can only book salons within 5 km.";
    }
    updateButton();
}

function watchLocation() {
    if (!navigator.geolocation) {
        rangeInfo.textContent = "Your browser can't share location, so booking isn't available here.";
        return;
    }
    navigator.geolocation.watchPosition(function (pos) { here = pos.coords; updateRange(); },
        function () { here = null; updateRange(); },
        { enableHighAccuracy: true, maximumAge: 15000, timeout: 20000 });
}

// ---- reservation timer -------------------------------------------------
function stopTimer() {
    if (holdTimer) { clearInterval(holdTimer); holdTimer = null; }
    holdInfo.hidden = true;
}

function startTimer() {
    stopTimer();
    function tick() {
        const left = Math.round((holdUntil - Date.now()) / 1000);
        if (left <= 0) {
            selected = null;
            stopTimer();
            showMessage("Your reservation expired. Please pick the time again.", false);
            updateButton();
            loadSlots();
            return;
        }
        holdInfo.hidden = false;
        holdInfo.textContent = "⏱ Reserved for you · " + Math.floor(left / 60) + ":" + String(left % 60).padStart(2, "0");
    }
    tick();
    if (selected) { holdTimer = setInterval(tick, 1000); }
}

function releaseHold() {
    if (selected) { post("/api/release").catch(function () { /* the hold expires by itself */ }); }
    selected = null;
    stopTimer();
}

async function selectSlot(slot) {
    if (auth !== "customer") {
        showMessage(auth === "" ? "Please sign in to book." : "Please use a customer account to book.", false);
        return;
    }
    try {
        const res = await post("/api/hold", { salon_id: salonId, slot: slot });
        const data = await res.json();
        if (res.ok) {
            selected = slot;
            holdUntil = Date.now() + data.seconds * 1000;
            message.hidden = true;
            startTimer();
        } else {
            selected = null;
            stopTimer();
            showMessage(data.error || "Couldn't reserve that time.", false);
        }
    } catch (e) {
        showMessage("Network problem. Please try again.", false);
    }
    updateButton();
    loadSlots();
}

// ---- slots ---------------------------------------------------------------
function render(data) {
    lastData = data;
    closedMsg.hidden = data.open;
    if (data.my_hold && !selected && data.my_hold.slot.slice(0, 10) === day) {     // came back to the page: restore
        selected = data.my_hold.slot;
        holdUntil = Date.now() + data.my_hold.seconds * 1000;
        startTimer();
    }
    if (selected && selected.slice(0, 10) === data.day) {
        const cur = data.slots.find(function (s) { return s.slot === selected; });
        if (!cur || cur.state !== "mine") {
            selected = null;
            stopTimer();
            showMessage("That time is no longer reserved for you. Please choose another.", false);
        }
    }
    grid.innerHTML = "";
    data.slots.forEach(function (s) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "slot " + s.state + (s.state === "mine" ? " selected" : "");
        b.textContent = pretty(s.time);
        b.disabled = s.state !== "free" && s.state !== "mine";
        b.title = s.state === "held" ? "Someone is booking this time right now" : "";
        if (s.state === "free") { b.addEventListener("click", function () { selectSlot(s.slot); }); }
        grid.appendChild(b);
    });
    updateButton();
}

async function loadSlots() {
    if (document.hidden) { return; }
    try {
        const res = await fetch("/api/salons/" + salonId + "/slots?day=" + encodeURIComponent(day), { credentials: "same-origin" });
        if (res.ok) { render(await res.json()); }
    } catch (e) { /* offline: keep showing the last times */ }
}

tabs.forEach(function (tab) {
    tab.addEventListener("click", function () {
        if (tab.dataset.day === day) { return; }
        if (selected) { releaseHold(); updateButton(); }
        day = tab.dataset.day;
        tabs.forEach(function (t) { t.classList.toggle("active", t === tab); });
        message.hidden = true;
        loadSlots();
    });
});

// ---- booking + confirmation pop-up -----------------------------------------
function waNumber(raw) {
    const digits = (raw || "").replace(/\D/g, "");
    return digits.length === 10 ? "91" + digits : digits;
}

function link(text, href, ghost) {
    const a = document.createElement("a");
    a.className = "btn" + (ghost ? " ghost" : "");
    a.textContent = text;
    a.href = href;
    if (href.indexOf("http") === 0) { a.target = "_blank"; a.rel = "noopener"; }
    return a;
}

function openModal(data) {
    document.getElementById("modalWhen").textContent = data.salon + " · " + data.when + " · " + data.service;
    document.getElementById("modalCode").textContent = data.code;
    const actions = document.getElementById("modalActions");
    actions.innerHTML = "";
    actions.appendChild(link("View booking", data.detail, false));
    if (data.directions) { actions.appendChild(link("🧭 Get directions", data.directions, true)); }
    if (data.owner_wa) {
        actions.appendChild(link("💬 WhatsApp the salon", "https://wa.me/" + data.owner_wa + "?text=" +
            encodeURIComponent("Hi, I booked " + data.when + " on Trimzee (booking #" + data.booking_id + ")."), true));
    }
    const me = waNumber(document.getElementById("phone").value);
    if (me.length >= 11) {
        actions.appendChild(link("📲 Save on my WhatsApp", "https://wa.me/" + me + "?text=" + encodeURIComponent(
            "Trimzee booking #" + data.booking_id + ": " + data.salon + ", " + data.when + ". Confirmation code: " + data.code), true));
    }
    modal.hidden = false;
    document.getElementById("modalClose").focus();
}

function closeModal() { modal.hidden = true; }
document.getElementById("modalClose").addEventListener("click", closeModal);
modal.addEventListener("click", function (e) { if (e.target === modal) { closeModal(); } });
document.addEventListener("keydown", function (e) { if (e.key === "Escape") { closeModal(); } });

confirmBtn.addEventListener("click", async function () {
    const service = document.querySelector("input[name=service]:checked");
    confirmBtn.disabled = true;
    try {
        const res = await post("/api/book", {
            salon_id: salonId, service_id: service ? service.value : null, slot: selected,
            phone: document.getElementById("phone").value,
            lat: here ? here.latitude : null, lng: here ? here.longitude : null
        });
        const data = await res.json();
        if (res.status === 201) {
            selected = null;
            stopTimer();
            message.hidden = true;
            openModal(data);
        } else {
            showMessage(data.error || "Something went wrong. Please try again.", false);
        }
    } catch (e) {
        showMessage("Network problem. Please try again.", false);
    }
    updateButton();
    loadSlots();
});

window.addEventListener("pagehide", function () {
    if (selected && navigator.sendBeacon) { navigator.sendBeacon("/api/release"); }
});
document.addEventListener("visibilitychange", function () { if (!document.hidden) { loadSlots(); } });

watchLocation();
updateRange();
loadSlots();
setInterval(loadSlots, POLL_MS);
