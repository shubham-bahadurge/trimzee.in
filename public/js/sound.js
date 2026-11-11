// Trimzee sound effects. Sounds are generated in the browser (no audio files).
// Sounds only play after the user has tapped something on the page, and can be muted.
(function () {
  var KEY = "trimzee_sound_muted";
  var ctx = null;
  var unlocked = false;

  function isMuted() {
    try { return localStorage.getItem(KEY) === "1"; } catch (e) { return false; }
  }

  function setMuted(value) {
    try { localStorage.setItem(KEY, value ? "1" : "0"); } catch (e) {}
    document.dispatchEvent(new CustomEvent("trimzee:sound-change", { detail: { muted: value } }));
  }

  function getContext() {
    if (!ctx) {
      var AudioCtx = window.AudioContext || window.webkitAudioContext;
      if (!AudioCtx) return null;
      ctx = new AudioCtx();
    }
    return ctx;
  }

  // Browsers only allow sound after a tap, so unlock on the first interaction.
  function unlock() {
    if (unlocked) return;
    var c = getContext();
    if (c && c.state === "suspended") c.resume();
    unlocked = true;
  }
  document.addEventListener("pointerdown", unlock, { once: true, passive: true });

  function tone(freq, start, duration, volume) {
    var c = getContext();
    if (!c) return;
    var osc = c.createOscillator();
    var gain = c.createGain();
    osc.type = "sine";
    osc.frequency.value = freq;
    gain.gain.setValueAtTime(0.0001, c.currentTime + start);
    gain.gain.exponentialRampToValueAtTime(volume, c.currentTime + start + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, c.currentTime + start + duration);
    osc.connect(gain);
    gain.connect(c.destination);
    osc.start(c.currentTime + start);
    osc.stop(c.currentTime + start + duration + 0.05);
  }

  // Two rising notes: booking confirmed.
  function playChime() {
    if (isMuted() || !unlocked) return;
    tone(660, 0, 0.18, 0.15);
    tone(880, 0.12, 0.25, 0.15);
  }

  // Three quick notes: new booking for the owner.
  function playAlert() {
    if (isMuted() || !unlocked) return;
    tone(740, 0, 0.12, 0.2);
    tone(740, 0.16, 0.12, 0.2);
    tone(988, 0.32, 0.22, 0.2);
  }

  window.TrimzeeSound = {
    playChime: playChime,
    playAlert: playAlert,
    isMuted: isMuted,
    setMuted: setMuted
  };
})();
