// Trimzee AI assistant widget
(function () {
    var btn = document.getElementById("chatBtn"), panel = document.getElementById("chatPanel");
    var log = document.getElementById("chatLog"), form = document.getElementById("chatForm");
    var input = document.getElementById("chatInput"), suggest = document.getElementById("chatSuggest");
    if (!btn) { return; }
    var greeted = false, busy = false;

    function bubble(text, who) {
        var el = document.createElement("div");
        el.className = "msg " + who;
        el.textContent = text;
        log.appendChild(el);
        log.scrollTop = log.scrollHeight;
        return el;
    }
    function cards(list) {
        if (!list || !list.length) { return; }
        var wrap = document.createElement("div");
        wrap.className = "msg-cards";
        list.forEach(function (s) {
            var a = document.createElement("a");
            a.href = "/salon/" + s.id;
            a.textContent = s.line;
            wrap.appendChild(a);
        });
        log.appendChild(wrap);
        log.scrollTop = log.scrollHeight;
    }
    async function ask(text) {
        if (busy || !text.trim()) { return; }
        busy = true;
        bubble(text, "me");
        var typing = bubble("…", "bot");
        try {
            var res = await fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ message: text }) });
            var data = await res.json();
            typing.textContent = data.reply || "Sorry, I couldn't answer that.";
            cards(data.salons);
        } catch (e) {
            typing.textContent = "I'm offline right now. Please try again in a moment.";
        }
        busy = false;
    }
    function open(show) {
        panel.hidden = !show;
        btn.setAttribute("aria-expanded", String(show));
        if (show && !greeted) {
            greeted = true;
            bubble("Hi! 👋 I'm the Trimzee assistant. Ask me about salons near you, prices, free slots or how to book.", "bot");
        }
        if (show) { input.focus(); }
    }
    btn.addEventListener("click", function (e) { e.stopPropagation(); open(panel.hidden); });
    document.getElementById("chatClose").addEventListener("click", function () { open(false); });
    panel.addEventListener("click", function (e) { e.stopPropagation(); });
    form.addEventListener("submit", function (e) {
        e.preventDefault();
        var text = input.value;
        input.value = "";
        ask(text);
    });
    suggest.querySelectorAll("button").forEach(function (b) {
        b.addEventListener("click", function () { ask(b.textContent); });
    });
})();
