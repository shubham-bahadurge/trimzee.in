"use strict";

window.trimzeeInstallPrompt = null;
window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    window.trimzeeInstallPrompt = event;
});

const menuButton = document.getElementById("menuBtn");
const menuPanel = document.getElementById("menuPanel");

if (menuButton && menuPanel) {
    function closeMenu() {
        menuPanel.hidden = true;
        menuButton.setAttribute("aria-expanded", "false");
    }

    menuButton.addEventListener("click", (event) => {
        event.stopPropagation();
        const open = menuPanel.hidden;
        menuPanel.hidden = !open;
        menuButton.setAttribute("aria-expanded", String(open));
    });
    menuPanel.addEventListener("click", (event) => event.stopPropagation());
    document.addEventListener("click", closeMenu);
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") { closeMenu(); }
    });
}
