/* Trimzee ad-slot adapter.
 * Real ads only render when a publisher supplies valid network or sponsor details.
 * Empty settings intentionally preserve the labelled placeholder.
 */
(function () {
  "use strict";

  var slots = document.querySelectorAll("[data-tz-ad-slot]");
  if (!slots.length) return;

  var adsenseScriptPromise = null;

  function validWebUrl(raw) {
    if (!raw) return null;
    try {
      var url = new URL(raw, window.location.href);
      if (url.protocol !== "https:" && url.protocol !== "http:") return null;
      return url.href;
    } catch (_) {
      return null;
    }
  }

  function activateSponsor(slot, root, placeholder) {
    var imageUrl = validWebUrl((slot.dataset.sponsorImage || "").trim());
    var destination = validWebUrl((slot.dataset.sponsorUrl || "").trim());
    if (!imageUrl || !destination) return false;

    var anchor = document.createElement("a");
    anchor.href = destination;
    anchor.target = "_blank";
    anchor.rel = "sponsored noopener noreferrer";
    anchor.setAttribute("aria-label", "Open sponsored partner website");

    var image = document.createElement("img");
    image.src = imageUrl;
    image.alt = (slot.dataset.sponsorAlt || "Sponsored partner").trim();
    image.loading = "lazy";
    image.decoding = "async";

    anchor.appendChild(image);
    root.replaceChildren(anchor);
    root.hidden = false;
    placeholder.hidden = true;
    return true;
  }

  function loadAdsenseScript(client) {
    if (adsenseScriptPromise) return adsenseScriptPromise;

    adsenseScriptPromise = new Promise(function (resolve, reject) {
      var existing = document.querySelector("script[data-tz-adsense-script]");
      if (existing) {
        if (existing.dataset.loaded === "true") resolve();
        else {
          existing.addEventListener("load", resolve, { once: true });
          existing.addEventListener("error", reject, { once: true });
        }
        return;
      }

      var script = document.createElement("script");
      script.async = true;
      script.crossOrigin = "anonymous";
      script.dataset.tzAdsenseScript = "true";
      script.src = "https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=" +
        encodeURIComponent(client);
      script.addEventListener("load", function () {
        script.dataset.loaded = "true";
        resolve();
      }, { once: true });
      script.addEventListener("error", reject, { once: true });
      document.head.appendChild(script);
    });

    return adsenseScriptPromise;
  }

  function activateAdsense(slot, root, placeholder) {
    var client = (slot.dataset.adsenseClient || "").trim();
    var adSlot = (slot.dataset.adsenseSlot || "").trim();

    // Never invent publisher credentials. Leave the placeholder in place until configured.
    if (!/^ca-pub-\d+$/.test(client) || !/^\d+$/.test(adSlot)) return;

    var ins = document.createElement("ins");
    ins.className = "adsbygoogle";
    ins.style.display = "block";
    ins.style.width = "100%";
    ins.dataset.adClient = client;
    ins.dataset.adSlot = adSlot;
    ins.dataset.adFormat = "auto";
    ins.dataset.fullWidthResponsive = "true";

    root.replaceChildren(ins);
    root.hidden = false;
    placeholder.hidden = true;

    loadAdsenseScript(client)
      .then(function () {
        (window.adsbygoogle = window.adsbygoogle || []).push({});
      })
      .catch(function () {
        // Advertising network failures should not break any Trimzee functionality.
        root.hidden = true;
        placeholder.hidden = false;
      });
  }

  slots.forEach(function (slot) {
    var placeholder = slot.querySelector("[data-ad-placeholder]");
    var root = slot.querySelector("[data-ad-render-root]");
    if (!placeholder || !root) return;

    if (activateSponsor(slot, root, placeholder)) return;
    activateAdsense(slot, root, placeholder);
  });
})();
