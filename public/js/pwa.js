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
let installPrompt = window.trimzeeInstallPrompt || null;
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
        window.trimzeeInstallPrompt = event;
        installButton.hidden = false;
    });

    installButton.addEventListener("click", async () => {
        if (!installPrompt) {
            installHelp.hidden = false;
            return;
        }
        const prompt = installPrompt;
        installPrompt = null;
        window.trimzeeInstallPrompt = null;
        try {
            await prompt.prompt();
            const choice = await prompt.userChoice;
            if (choice.outcome === "accepted") {
                installButton.hidden = true;
            } else {
                installHelp.hidden = false;
            }
        } catch (error) {
            console.error("Trimzee install prompt failed:", error);
            installHelp.textContent = "Could not open the install prompt. Use your browser's menu to install Trimzee.";
            installHelp.hidden = false;
        }
    });

    window.addEventListener("appinstalled", () => {
        installPrompt = null;
        window.trimzeeInstallPrompt = null;
        installButton.hidden = true;
    });
}

const pushButton = document.getElementById("enablePush");
const pushHelp = document.getElementById("pushHelp");

if (pushButton && "serviceWorker" in navigator && "PushManager" in window && "Notification" in window) {
    if (isIos && !isStandalone) {
        pushHelp.textContent = "On iPhone or iPad, first install Trimzee from Safari's Share menu, then enable notifications from the installed app.";
        pushHelp.hidden = false;
    } else {
        fetch("/api/push/config", { credentials: "same-origin" })
        .then((response) => response.json())
        .then((config) => {
            if (!config.available) {
                pushHelp.textContent = "Push notifications are not configured yet.";
                pushHelp.hidden = false;
                return;
            }
            pushButton.hidden = false;
            navigator.serviceWorker.ready.then((registration) =>
                registration.pushManager.getSubscription()
            ).then((subscription) => {
                pushButton.textContent = subscription ? "Turn off device notifications" : "Enable notifications";
            }).catch((error) => {
                console.error("Could not check device notification subscription.", error);
            });
            pushButton.addEventListener("click", async () => {
                pushButton.disabled = true;
                pushHelp.hidden = true;
                try {
                    const registration = await navigator.serviceWorker.ready;
                    let subscription = await registration.pushManager.getSubscription();
                    if (subscription) {
                        const response = await fetch("/api/push/unsubscribe", {
                            method: "POST",
                            credentials: "same-origin",
                            headers: { "Content-Type": "application/json", "X-CSRF-Token": window.TZ_CSRF },
                            body: JSON.stringify({ endpoint: subscription.endpoint })
                        });
                        if (!response.ok) {
                            const result = await response.json();
                            throw new Error(result.error || "Could not turn off notifications.");
                        }
                        await subscription.unsubscribe();
                        pushButton.textContent = "Enable notifications";
                        pushHelp.textContent = "Device notifications are off.";
                        pushHelp.hidden = false;
                        return;
                    }
                    if (Notification.permission !== "granted") {
                        const permission = await Notification.requestPermission();
                        if (permission !== "granted") {
                            pushHelp.textContent = "Allow notifications in your browser settings to receive updates.";
                            pushHelp.hidden = false;
                            return;
                        }
                    }
                    const encodedKey = config.public_key.replace(/-/g, "+").replace(/_/g, "/");
                    const key = Uint8Array.from(atob(encodedKey.padEnd(Math.ceil(encodedKey.length / 4) * 4, "=")),
                        (character) => character.charCodeAt(0));
                    subscription = await registration.pushManager.subscribe({
                        userVisibleOnly: true,
                        applicationServerKey: key
                    });
                    const response = await fetch("/api/push/subscribe", {
                        method: "POST",
                        credentials: "same-origin",
                        headers: { "Content-Type": "application/json", "X-CSRF-Token": window.TZ_CSRF },
                        body: JSON.stringify(subscription.toJSON())
                    });
                    if (!response.ok) {
                        const result = await response.json();
                        throw new Error(result.error || "Could not enable notifications.");
                    }
                    pushButton.textContent = "Turn off device notifications";
                    pushHelp.textContent = "Trimzee can now notify this device about booking updates.";
                    pushHelp.hidden = false;
                } catch (error) {
                    pushHelp.textContent = error.message || "Could not enable notifications.";
                    pushHelp.hidden = false;
                } finally {
                    pushButton.disabled = false;
                }
            });
        })
        .catch((error) => {
            console.error("Could not check push notification setup.", error);
            pushHelp.textContent = "Could not check notification settings. Please try again later.";
            pushHelp.hidden = false;
        });
    }
} else if (pushButton) {
    pushHelp.textContent = "Device push notifications are not supported by this browser.";
    pushHelp.hidden = false;
}
