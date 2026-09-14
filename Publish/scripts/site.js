/* Mondi.tech: progressive enhancement for the static Publish build.
   Copied verbatim to Publish/scripts/site.js by scripts/build-publish.py. Do not edit the Publish copy.

   All page content already exists in the HTML. This file only adds the interactions that the
   homepage DC logic class ("Mondi.tech Homepage.dc.html") implemented at runtime:
     [data-reveal]       Impact section reveal/un-reveal on scroll (IntersectionObserver)
     [data-carousel]     client logo carousel: mouse drag + edge-hover auto-scroll
     [data-form-toggle]  collapsible contact form
     #cookie-consent     cookie consent banner (<template>, shown until a choice is stored)
   Behaviour and constants mirror the DC source; keep them in sync if that logic changes. */
(function () {
  'use strict';

  function setLabels(root, on) {
    var els = root.hasAttribute('data-label-on') ? [root] : root.querySelectorAll('[data-label-on]');
    Array.prototype.forEach.call(els, function (el) {
      el.textContent = el.getAttribute(on ? 'data-label-on' : 'data-label-off');
    });
  }

  // Removes/re-inserts a node in place (the DC runtime used sc-if, which mounts/unmounts).
  // A display toggle is not used because the approved CSS sets display on these elements.
  function mountable(el, name) {
    var slot = document.createComment(name);
    el.parentNode.insertBefore(slot, el);
    return function (show) {
      if (show && !el.parentNode) slot.parentNode.insertBefore(el, slot.nextSibling);
      else if (!show && el.parentNode) el.parentNode.removeChild(el);
    };
  }

  // Impact: reveal when the section scrolls into view; un-reveal when it leaves so the
  // sequence replays on every pass. Reduced motion is handled in mondi.css.
  Array.prototype.forEach.call(document.querySelectorAll('[data-reveal]'), function (section) {
    if (!('IntersectionObserver' in window)) { section.classList.add('is-revealed'); return; }
    var threshold = parseFloat(section.getAttribute('data-reveal-threshold')) || 0;
    new IntersectionObserver(function (entries) {
      entries.forEach(function (e) { section.classList.toggle('is-revealed', e.isIntersecting); });
    }, { threshold: threshold }).observe(section);
  });

  // Client carousel: drag with the mouse, auto-scroll while hovering near either edge.
  Array.prototype.forEach.call(document.querySelectorAll('[data-carousel]'), function (el) {
    var drag = null, autoDir = 0, autoRaf = null;
    function setAuto(dir) {
      if (dir === autoDir) return;
      autoDir = dir;
      el.style.scrollSnapType = dir ? 'none' : 'x proximity';
      if (autoRaf) { cancelAnimationFrame(autoRaf); autoRaf = null; }
      if (!dir) return;
      var step = function () {
        if (!autoDir) return;
        el.scrollLeft += autoDir * 2.2;
        autoRaf = requestAnimationFrame(step);
      };
      autoRaf = requestAnimationFrame(step);
    }
    function dragEnd() {
      drag = null;
      el.style.cursor = 'grab';
      el.style.scrollSnapType = 'x proximity';
    }
    el.addEventListener('pointerdown', function (e) {
      if (e.pointerType !== 'mouse') return;
      setAuto(0);
      drag = { x: e.clientX, left: el.scrollLeft, moved: false };
      el.style.cursor = 'grabbing';
      el.style.scrollSnapType = 'none';
    });
    el.addEventListener('pointermove', function (e) {
      if (!drag) return;
      var dx = e.clientX - drag.x;
      if (Math.abs(dx) > 3) drag.moved = true;
      el.scrollLeft = drag.left - dx;
    });
    el.addEventListener('pointerup', dragEnd);
    el.addEventListener('pointerleave', dragEnd);
    el.addEventListener('pointercancel', dragEnd);
    el.addEventListener('mousemove', function (e) {
      if (drag) return;
      var b = el.getBoundingClientRect();
      var zone = Math.min(200, b.width * 0.22);
      setAuto(e.clientX < b.left + zone ? -1 : e.clientX > b.right - zone ? 1 : 0);
    });
    el.addEventListener('mouseleave', function () { setAuto(0); });
  });

  // Contact form: the fields are in the HTML (usable without JS); collapse to the DC initial state.
  Array.prototype.forEach.call(document.querySelectorAll('[data-form-toggle]'), function (toggle) {
    var fields = document.getElementById(toggle.getAttribute('aria-controls'));
    if (!fields) return;
    var show = mountable(fields, 'contact-fields');
    var open;
    function render(next) {
      open = next;
      toggle.setAttribute('aria-expanded', String(open));
      setLabels(toggle, open);
      show(open);
    }
    render(toggle.getAttribute('data-form-toggle') === 'open');
    toggle.addEventListener('click', function () { render(!open); });
  });

  // Cookie consent banner.
  var tpl = document.getElementById('cookie-consent');
  if (tpl && tpl.content) {
    var key = tpl.getAttribute('data-consent-key');
    var stored = null;
    try { stored = localStorage.getItem(key); } catch (e) {}
    if (!stored) {
      var banner = tpl.content.firstElementChild.cloneNode(true);
      var prefs = banner.querySelector('[data-cookie-prefs]');
      var boxes = prefs.querySelectorAll('[data-cookie-category]');
      var showPrefs = mountable(prefs, 'cookie-prefs');
      var prefsOpen = false;
      showPrefs(false);

      var categories = function (override) {
        var out = {};
        Array.prototype.forEach.call(boxes, function (b) {
          out[b.getAttribute('data-cookie-category')] = b.disabled || override === undefined ? b.checked : override;
        });
        return out;
      };
      var save = function (cats) {
        try { localStorage.setItem(key, JSON.stringify(cats)); } catch (e) {}
        if (banner.parentNode) banner.parentNode.removeChild(banner);
      };
      banner.addEventListener('click', function (e) {
        var btn = e.target.closest('[data-cookie-action]');
        if (!btn) return;
        var action = btn.getAttribute('data-cookie-action');
        if (action === 'accept') save(prefsOpen ? categories() : categories(true));
        else if (action === 'deny') save(categories(false));
        else if (action === 'prefs') {
          prefsOpen = !prefsOpen;
          showPrefs(prefsOpen);
          setLabels(banner, prefsOpen);
        }
      });
      tpl.parentNode.insertBefore(banner, tpl);
    }
  }
})();
