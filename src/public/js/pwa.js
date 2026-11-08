"use strict";

if (!document.querySelector('link[rel="manifest"]')) {
    const manifest = document.createElement("link");
    manifest.rel = "manifest";
    manifest.href = "/manifest.webmanifest";
    document.head.appendChild(manifest);
}

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
    installButton.hidden = false;
    if (isIos) {
        installButton.textContent = "Add Trimzee to Home Screen";
        installHelp.textContent = "In Safari, tap Share, then choose Add to Home Screen.";
    } else {
        installHelp.textContent = "Use your browser's menu and choose Install Trimzee or Add to Home Screen.";
    }

    window.addEventListener("beforeinstallprompt", (event) => {
        event.preventDefault();
        installPrompt = event;
        installButton.hidden = false;
    });

    installButton.addEventListener("click", async () => {
        if (!installPrompt) {
            installHelp.hidden = false;
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
