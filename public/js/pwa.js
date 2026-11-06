"use strict";

if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/service-worker.js")
        .catch((error) => console.error("Trimzee service worker registration failed:", error));
}

const installButton = document.getElementById("installApp");
const installHelp = document.getElementById("installHelp");
let installPrompt;
const isStandalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone;
const isIos = /iphone|ipad|ipod/i.test(navigator.userAgent)
    || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);

if (installButton && !isStandalone) {
    if (isIos) {
        installButton.textContent = "Add Trimzee to Home Screen";
        installButton.hidden = false;
        installButton.addEventListener("click", () => {
            installHelp.hidden = false;
        });
    }

    window.addEventListener("beforeinstallprompt", (event) => {
        event.preventDefault();
        installPrompt = event;
        installButton.hidden = false;
    });

    installButton.addEventListener("click", async () => {
        if (!installPrompt) {
            return;
        }
        installPrompt.prompt();
        await installPrompt.userChoice;
        installPrompt = null;
        installButton.hidden = true;
    });

    window.addEventListener("appinstalled", () => {
        installPrompt = null;
        installButton.hidden = true;
    });
}
