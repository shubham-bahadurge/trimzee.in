// Trimzee homepage: search, live location (salons within 5 km), male/female filter for guests
const RANGE_KM = 5;
const searchInput = document.getElementById("search");
const grid = document.getElementById("grid");
const cards = Array.from(document.querySelectorAll(".card"));
const emptyMsg = document.getElementById("empty");
const locBtn = document.getElementById("useLocation");
const locStatus = document.getElementById("locStatus");
const genderBtn = document.getElementById("genderBtn");
const genderPanel = document.getElementById("genderPanel");
const savedView = document.body.dataset.savedView === "true";
const savedStorageKey = "trimzee.savedSalons";

let activeGender = "";
let here = null;

function readSavedSalons() {
    try {
        const saved = JSON.parse(localStorage.getItem(savedStorageKey) || "[]");
        return Array.isArray(saved) ? saved.map(String) : [];
    } catch (error) {
        console.error("Could not read saved salons from browser storage.", error);
        return [];
    }
}

let savedSalons = readSavedSalons();

function updateSaveButtons() {
    cards.forEach(function (card) {
        const button = card.querySelector(".save-salon");
        const isSaved = savedSalons.includes(card.dataset.salonId);
        button.setAttribute("aria-pressed", String(isSaved));
        button.setAttribute("aria-label", (isSaved ? "Remove " : "Save ") + card.dataset.name);
        button.title = isSaved ? "Remove saved salon" : "Save salon";
    });
}

function haversineKm(lat1, lon1, lat2, lon2) {
    const rad = Math.PI / 180;
    const dLat = (lat2 - lat1) * rad;
    const dLon = (lon2 - lon1) * rad;
    const a = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
        Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(dLon / 2) * Math.sin(dLon / 2);
    return 6371 * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

function applyFilters() {
    const text = searchInput.value.trim().toLowerCase();
    let visible = 0;
    cards.forEach(function (card) {
        const matchText = !text || card.dataset.name.includes(text) || card.dataset.services.includes(text);
        const gender = card.dataset.gender;
        const matchGender = !activeGender || gender === activeGender || gender === "unisex";
        const matchRange = !here || (card.dataset.known === "1" && parseFloat(card.dataset.distance) <= RANGE_KM);
        const matchSaved = !savedView || savedSalons.includes(card.dataset.salonId);
        const show = matchText && matchGender && matchRange && matchSaved;
        card.hidden = !show;
        if (show) { visible += 1; }
    });
    emptyMsg.textContent = savedView ? "No saved salons yet. Tap the heart on a salon to save it." :
        here ? "No salons within 5 km of you yet. Try moving closer or check back soon." :
            "No salons match here yet. Try another search.";
    emptyMsg.hidden = visible > 0;
}

function updateDistances() {
    cards.forEach(function (card) {
        const lat = parseFloat(card.dataset.lat);
        const lng = parseFloat(card.dataset.lng);
        const label = card.querySelector(".dist");
        if (isNaN(lat) || isNaN(lng)) {
            card.dataset.known = "0";
            card.dataset.distance = "99999";
            label.textContent = "?";
        } else {
            const km = haversineKm(here.latitude, here.longitude, lat, lng);
            card.dataset.known = "1";
            card.dataset.distance = km.toFixed(1);
            label.textContent = km.toFixed(1);
        }
    });
    cards.sort(function (a, b) { return parseFloat(a.dataset.distance) - parseFloat(b.dataset.distance); })
        .forEach(function (card) { grid.appendChild(card); });
    locStatus.textContent = "📍 Live location on · showing salons within 5 km, nearest first";
    applyFilters();
}

let watchId = null;
function startLocation() {
    if (!navigator.geolocation) {
        locStatus.textContent = "Your browser can't share location, so we're showing all salons.";
        return;
    }
    locStatus.textContent = "Finding your location…";
    if (watchId !== null) { navigator.geolocation.clearWatch(watchId); }
    watchId = navigator.geolocation.watchPosition(function (pos) {
        here = pos.coords;
        updateDistances();
    }, function () {
        here = null;
        locStatus.textContent = "📍 Location is off. Tap the pin and allow access to see salons within 5 km.";
        applyFilters();
    }, { enableHighAccuracy: true, maximumAge: 15000, timeout: 20000 });
}

locBtn.addEventListener("click", startLocation);
searchInput.addEventListener("input", applyFilters);
if (!savedView) { startLocation(); }

document.querySelectorAll(".book").forEach(function (btn) {
    btn.addEventListener("click", function () { window.location.href = "/salon/" + btn.dataset.id; });
});

document.querySelectorAll(".save-salon").forEach(function (btn) {
    btn.addEventListener("click", function () {
        const salonId = btn.closest(".card").dataset.salonId;
        savedSalons = savedSalons.includes(salonId)
            ? savedSalons.filter(function (id) { return id !== salonId; })
            : savedSalons.concat(salonId);
        try {
            localStorage.setItem(savedStorageKey, JSON.stringify(savedSalons));
        } catch (error) {
            console.error("Could not save salons to browser storage.", error);
        }
        updateSaveButtons();
        applyFilters();
    });
});
updateSaveButtons();
applyFilters();

if (genderBtn) {
    genderBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        genderPanel.hidden = !genderPanel.hidden;
        genderBtn.setAttribute("aria-expanded", String(!genderPanel.hidden));
    });
    genderPanel.addEventListener("click", function (e) { e.stopPropagation(); });
    genderPanel.querySelectorAll(".gender").forEach(function (btn) {
        btn.addEventListener("click", function () {
            activeGender = btn.dataset.gender;
            genderPanel.querySelectorAll(".gender").forEach(function (b) { b.classList.remove("active"); });
            btn.classList.add("active");
            genderBtn.classList.toggle("on", activeGender !== "");
            genderPanel.hidden = true;
            genderBtn.setAttribute("aria-expanded", "false");
            applyFilters();
        });
    });
    document.addEventListener("click", function () { genderPanel.hidden = true; });
}
