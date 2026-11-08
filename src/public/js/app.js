// Trimzee homepage: search, live location (salons within 5 km), male/female filter for guests
const RANGE_KM = 5;
const searchInput = document.getElementById("search");
const grid = document.getElementById("grid");
const cards = Array.from(document.querySelectorAll(".card"));
const emptyMsg = document.getElementById("empty");
const locBtn = document.getElementById("useLocation");
const locStatus = document.getElementById("locStatus");
const locationPicker = document.getElementById("locationPicker");
const mapElement = document.getElementById("nearbyMap");
const mapMessage = document.getElementById("mapMessage");
const savedView = document.body.dataset.savedView === "true";
const savedStorageKey = "trimzee.savedSalons";

let here = null;
let nearbyMap = null;
let mapMarkers = new Map();
let userMarker = null;
let rangeCircle = null;
let leafletPromise = null;

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
    const locationKey = locationPicker ? locationPicker.value : "";
    let visible = 0;
    cards.forEach(function (card) {
        const matchText = !text || card.dataset.name.includes(text) || card.dataset.services.includes(text);
        const [locationType, ...locationParts] = locationKey.split(":");
        const selectedPlace = locationParts.join(":");
        const [selectedArea, selectedCity] = selectedPlace.split("|");
        const matchLocation = !locationKey || (locationType === "city"
            ? card.dataset.city === selectedPlace
            : card.dataset.area === selectedArea && (!selectedCity || card.dataset.city === selectedCity));
        const matchRange = locationKey || !here || (card.dataset.known === "1" && parseFloat(card.dataset.distance) <= RANGE_KM);
        const matchSaved = !savedView || savedSalons.includes(card.dataset.salonId);
        const show = matchText && matchLocation && matchRange && matchSaved;
        card.hidden = !show;
        if (show) { visible += 1; }
    });
    emptyMsg.textContent = savedView ? "No saved salons yet. Tap the heart on a salon to save it." :
        locationKey ? "No salons listed in " + locationPicker.options[locationPicker.selectedIndex].text + " yet. Choose another area or browse all salons." :
            here ? "No salons within 5 km of you yet. Try moving closer or check back soon." :
            "No salons match here yet. Try another search.";
    emptyMsg.hidden = visible > 0;
    updateMap();
}

function updateDistances() {
    cards.forEach(function (card) {
        const lat = parseFloat(card.dataset.lat);
        const lng = parseFloat(card.dataset.lng);
        const label = card.querySelector(".dist");
        if (isNaN(lat) || isNaN(lng)) {
            card.dataset.known = "0";
            card.dataset.distance = "99999";
            label.textContent = "Location unavailable";
        } else {
            const km = haversineKm(here.latitude, here.longitude, lat, lng);
            card.dataset.known = "1";
            card.dataset.distance = String(km);
            label.textContent = km.toFixed(1) + " km away";
        }
    });
    cards.slice().sort(function (a, b) { return parseFloat(a.dataset.distance) - parseFloat(b.dataset.distance); })
        .forEach(function (card) { grid.appendChild(card); });
    locBtn.classList.add("on");
    locStatus.textContent = "📍 Live location on · showing salons within 5 km, nearest first";
    applyFilters();
}

function resetDistances() {
    cards.forEach(function (card) {
        const label = card.querySelector(".dist");
        label.textContent = card.dataset.locationLabel || "Use live location for distance";
        card.dataset.known = "0";
        card.dataset.distance = "99999";
    });
    cards.forEach(function (card) { grid.appendChild(card); });
}

function statusColor(status) {
    return { available: "#15803d", busy: "#d97706", full: "#dc2626", closed: "#64748b" }[status] || "#64748b";
}

function loadLeaflet() {
    if (window.L) { return Promise.resolve(); }
    if (leafletPromise) { return leafletPromise; }
    leafletPromise = new Promise(function (resolve, reject) {
        const css = document.createElement("link");
        css.rel = "stylesheet";
        css.href = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css";
        css.integrity = "sha384-sHL9NAb7lN7rfvG5lfHpm643Xkcjzp4jFvuavGOndn6pjVqS6ny56CAt3nsEVT4H";
        css.crossOrigin = "anonymous";
        document.head.appendChild(css);

        const script = document.createElement("script");
        script.src = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js";
        script.integrity = "sha384-cxOPjt7s7Iz04uaHJceBmS+qpjv2JkIHNVcuOrM+YHwZOmJGBXI00mdUXEq65HTH";
        script.crossOrigin = "anonymous";
        script.onload = resolve;
        script.onerror = function () { reject(new Error("Leaflet failed to load")); };
        document.head.appendChild(script);
    }).catch(function (error) {
        leafletPromise = null;
        throw error;
    });
    return leafletPromise;
}

function createMapPopup(card) {
    const popup = document.createElement("div");
    const name = document.createElement("strong");
    name.textContent = card.querySelector(".salon-card-heading h3").textContent;
    const detail = document.createElement("p");
    const status = card.querySelector(".badge").textContent;
    detail.textContent = card.querySelector(".dist").textContent + " km away · " + status;
    const link = document.createElement("a");
    link.href = "/salon/" + encodeURIComponent(card.dataset.salonId);
    link.textContent = "View salon";
    popup.append(name, detail, link);
    return popup;
}

function updateMap() {
    if (!mapElement) { return; }
    if (!here) {
        if (nearbyMap) { nearbyMap.remove(); }
        nearbyMap = null;
        mapMarkers.clear();
        rangeCircle = null;
        userMarker = null;
        if (mapMessage) {
            mapMessage.textContent = "Use live location to see nearby salons on the map.";
            mapMessage.hidden = false;
        }
        return;
    }
    if (!window.L) {
        mapMessage.textContent = "Loading nearby map…";
        mapMessage.hidden = false;
        loadLeaflet().then(updateMap).catch(function () {
            mapMessage.textContent = "The map could not be loaded. Please try again later.";
            mapMessage.hidden = false;
        });
        return;
    }
    if (!nearbyMap) {
        const zoom = mapElement.clientWidth < 400 ? 11 : 12;
        nearbyMap = L.map(mapElement, { scrollWheelZoom: false }).setView([here.latitude, here.longitude], zoom);
        L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
            maxZoom: 19
        }).addTo(nearbyMap);
        rangeCircle = L.circle([here.latitude, here.longitude], {
            radius: RANGE_KM * 1000,
            color: "#119b8a",
            fillColor: "#119b8a",
            fillOpacity: 0.08,
            weight: 2
        }).addTo(nearbyMap);
        userMarker = L.circleMarker([here.latitude, here.longitude], {
            radius: 7,
            color: "#fff",
            weight: 2,
            fillColor: "#2563eb",
            fillOpacity: 1
        }).bindPopup("Your location").addTo(nearbyMap);
        window.setTimeout(function () { nearbyMap.invalidateSize(); }, 0);
    } else {
        rangeCircle.setLatLng([here.latitude, here.longitude]);
        userMarker.setLatLng([here.latitude, here.longitude]);
    }

    const visibleIds = new Set();
    cards.forEach(function (card) {
        if (card.hidden || card.dataset.known !== "1" || Number(card.dataset.distance) > RANGE_KM) { return; }
        const id = card.dataset.salonId;
        const position = [Number(card.dataset.lat), Number(card.dataset.lng)];
        const color = statusColor(card.dataset.status);
        visibleIds.add(id);
        let marker = mapMarkers.get(id);
        if (!marker) {
            marker = L.circleMarker(position, {
                radius: 9,
                color: "#fff",
                weight: 2,
                fillOpacity: 1
            }).addTo(nearbyMap);
            mapMarkers.set(id, marker);
        }
        marker.setLatLng(position);
        marker.setStyle({ fillColor: color });
        marker.bindPopup(createMapPopup(card));
    });
    mapMarkers.forEach(function (marker, id) {
        if (!visibleIds.has(id)) {
            marker.remove();
            mapMarkers.delete(id);
        }
    });
    mapMessage.textContent = visibleIds.size
        ? ""
        : "No salons match within 5 km. Try another search.";
    mapMessage.hidden = visibleIds.size > 0;
}

let watchId = null;
function startLocation() {
    if (locationPicker) { locationPicker.value = ""; }
    here = null;
    resetDistances();
    locBtn.classList.remove("on");
    applyFilters();
    if (!navigator.geolocation) {
        locStatus.textContent = "Your browser can't share location, so browse all salons or choose a city or area.";
        if (mapMessage) {
            mapMessage.textContent = "Your browser can't share location, so nearby salons can't be shown on the map.";
        }
        return;
    }
    locStatus.textContent = "Finding your location…";
    if (mapMessage) {
        mapMessage.textContent = "Finding your location…";
        mapMessage.hidden = false;
    }
    if (watchId !== null) { navigator.geolocation.clearWatch(watchId); }
    watchId = navigator.geolocation.watchPosition(function (pos) {
        here = pos.coords;
        updateDistances();
    }, function () {
        here = null;
        resetDistances();
        locBtn.classList.remove("on");
        locStatus.textContent = "📍 Location is off. Browse all salons or choose a city or area.";
        if (mapMessage) {
            mapMessage.textContent = "Allow location access to see nearby salons on the map.";
            mapMessage.hidden = false;
        }
        applyFilters();
    }, { enableHighAccuracy: true, maximumAge: 15000, timeout: 20000 });
}

locBtn.addEventListener("click", startLocation);
searchInput.addEventListener("input", applyFilters);
if (locationPicker) {
    locationPicker.addEventListener("change", function () {
        if (watchId !== null) {
            navigator.geolocation.clearWatch(watchId);
            watchId = null;
        }
        here = null;
        locBtn.classList.remove("on");
        resetDistances();
        locStatus.textContent = locationPicker.value
            ? "Showing salons in " + locationPicker.options[locationPicker.selectedIndex].text + "."
            : "Showing all salons. Choose a city or area, or use live location.";
        applyFilters();
    });
}

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
