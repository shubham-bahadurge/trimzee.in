// Trimzee salon page: live slots, 5 km live-location check, booking + confirmation code
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
const tabs = Array.from(document.querySelectorAll(".day-tab"));
const modal = document.getElementById("modal");

let day = tabs.length ? tabs[0].dataset.day : "";
let selected = null;
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
        confirmBtn.textContent = "Customer account needed";
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

// ---- slot selection -----------------------------------------------------
function selectSlot(slot) {
    if (auth !== "customer") {
        showMessage(auth === "" ? "Please sign in to book." : "Please use a customer account to book.", false);
        return;
    }
    selected = slot;
    message.hidden = true;
    updateButton();
    if (lastData) { render(lastData); }
}

// ---- slots ---------------------------------------------------------------
function render(data) {
    lastData = data;
    closedMsg.hidden = data.open;
    if (selected && selected.slice(0, 10) === data.day) {
        const cur = data.slots.find(function (s) { return s.slot === selected; });
        if (!cur || cur.state !== "free") {
            selected = null;
            showMessage("That time is no longer available. Please choose another.", false);
        }
    }
    grid.innerHTML = "";
    data.slots.forEach(function (s) {
        const b = document.createElement("button");
        b.type = "button";
        const isSelected = s.slot === selected;
        b.className = "slot " + (isSelected ? "selected" : s.state);
        b.textContent = pretty(s.time);
        b.disabled = s.state !== "free";
        b.setAttribute("aria-pressed", isSelected ? "true" : "false");
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
        selected = null;
        updateButton();
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

document.addEventListener("visibilitychange", function () { if (!document.hidden) { loadSlots(); } });

watchLocation();
updateRange();
loadSlots();
setInterval(loadSlots, POLL_MS);
