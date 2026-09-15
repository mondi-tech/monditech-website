(function () {
  'use strict';
  var ENDPOINT = 'https://api.web3forms.com/submit';
  // Status text follows the page language (<html lang="hu"> on the Hungarian site).
  var TEXT = {
    en: {
      invalid: 'Please enter your name, a valid email address, and a message.',
      sending: 'Sending your message…',
      success: 'Thank you! Your message has been sent successfully.',
      failure: 'Something went wrong while sending your message. Please try again, or call us at +36 20 482 6070.'
    },
    hu: {
      invalid: 'Kérjük, adja meg a nevét, egy érvényes e-mail-címet és az üzenetét.',
      sending: 'Üzenet küldése…',
      success: 'Köszönjük! Üzenetét sikeresen elküldtük.',
      failure: 'Az üzenet elküldése nem sikerült. Kérjük, próbálja újra, vagy hívjon minket a +36 20 482 6070-es számon.'
    }
  };
  var lang = /^hu\b/i.test(document.documentElement.lang || '') ? 'hu' : 'en';
  var text = TEXT[lang];
  // Delegation also handles fields mounted later by the DC runtime.
  function enableForms() {
    document.querySelectorAll('[data-contact-form]:not([data-sending]) button[type="submit"]').forEach(function (button) {
      button.disabled = false;
    });
  }
  new MutationObserver(enableForms).observe(document.documentElement, { childList: true, subtree: true });
  enableForms();
  document.addEventListener('submit', async function (event) {
    var form = event.target;
    if (!form.matches('[data-contact-form]')) return;
    event.preventDefault();
    if (form.hasAttribute('data-sending')) return;
    var status = form.querySelector('[data-contact-status]');
    function tell(message) { status.hidden = false; status.textContent = message; }
    var data = new FormData(form);
    var field = function (key) { return String(data.get(key) || '').trim(); };
    if (!form.reportValidity() || !field('name') || !field('message')) {
      tell(text.invalid);
      return;
    }
    // Web3Forms honeypot: only bots tick the hidden botcheck box. Pretend success without sending.
    if (data.get('botcheck')) {
      form.reset();
      tell(text.success);
      return;
    }
    var button = form.querySelector('button[type="submit"]');
    form.setAttribute('data-sending', '');
    button.disabled = true;
    tell(text.sending);
    var controller = new AbortController();
    var timeout = setTimeout(function () { controller.abort(); }, 20000);
    try {
      var key = window.MONDI_CONTACT_KEY;
      if (!key) throw new Error('Contact form access key is not configured');
      var response = await fetch(ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({
          access_key: key,
          subject: 'New message from the mondi.tech contact form (' + lang.toUpperCase() + ')',
          from_name: 'Mondi.tech website',
          // Web3Forms uses the submitted "email" field as the Reply-To address.
          name: field('name'),
          email: field('email'),
          message: field('message')
        }),
        credentials: 'omit', signal: controller.signal
      });
      var result = await response.json();
      if (!response.ok || result.success !== true) throw new Error('Not accepted');
      form.reset();
      tell(text.success);
    } catch (error) {
      tell(text.failure);
    } finally {
      clearTimeout(timeout);
      form.removeAttribute('data-sending');
      button.disabled = false;
    }
  }, true);
})();
