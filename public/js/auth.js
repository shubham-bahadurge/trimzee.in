"use strict";

document.querySelectorAll("[data-password-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
        const input = document.getElementById(button.dataset.passwordToggle);
        if (!input) { return; }
        const visible = input.type === "password";
        input.type = visible ? "text" : "password";
        button.setAttribute("aria-pressed", String(visible));
        button.setAttribute("aria-label", visible ? "Hide password" : "Show password");
    });
});

const salonField = document.getElementById("salonField");
const phoneField = document.getElementById("phoneField");
if (salonField && phoneField) {
    function syncRegistrationFields() {
        const owner = document.querySelector("input[name=role]:checked").value === "owner";
        const female = document.querySelector("input[name=gender]:checked");
        const word = female && female.value === "female" ? "parlour" : "salon";
        salonField.hidden = !owner;
        phoneField.hidden = !owner;
        phoneField.querySelector("input").required = owner;
        document.getElementById("bizLabel").textContent = word.charAt(0).toUpperCase() + word.slice(1) + " name";
    }

    document.querySelectorAll("input[name=role], input[name=gender]").forEach((input) => {
        input.addEventListener("change", syncRegistrationFields);
    });
    syncRegistrationFields();
}
