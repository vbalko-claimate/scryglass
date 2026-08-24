/* Scryglass App Shell & Shared Account Handler */
(function() {
  // ── Update notice (app window, PRIMARY surface) ────────────────────────────
  // The Tauri shell eval()s `showUpdateNotice('x.y.z')` into the main window
  // when the updater finds a new version (on launch + every 4 h). USER-REPORTED
  // 2026-08-19: the tray-only advert was invisible — a tester sat on an old
  // version for days. Injected by the shared shell so it shows on every app
  // page; dismissible (a real window, unlike the overlay), and a dismissed
  // version stays dismissed until a NEWER one appears.
  let dismissedUpdate = null;
  window.showUpdateNotice = function (version) {
    if (version === dismissedUpdate) return;
    let bar = document.getElementById('shell-update-notice');
    if (!bar) {
      bar = document.createElement('div');
      bar.id = 'shell-update-notice';
      bar.style.cssText =
        'position:fixed;top:0;left:0;right:0;z-index:9999;display:flex;' +
        'align-items:center;justify-content:center;gap:12px;padding:8px 14px;' +
        'background:#ffd166;color:#0f1117;font-weight:600;font-size:14px;';
      const txt = document.createElement('span');
      txt.id = 'shell-update-notice-text';
      const btn = document.createElement('button');
      btn.textContent = 'Dismiss';
      btn.style.cssText =
        'border:none;border-radius:4px;padding:2px 10px;cursor:pointer;' +
        'background:rgba(15,17,23,0.15);color:#0f1117;font-weight:600;';
      btn.addEventListener('click', () => {
        dismissedUpdate = version;
        bar.remove();
      });
      bar.appendChild(txt);
      bar.appendChild(btn);
      document.body.appendChild(bar);
    }
    document.getElementById('shell-update-notice-text').textContent =
      'Update v' + version + ' is ready — install from the Scryglass tray menu (Install Update v' + version + '…)';
  };

  // ── What's changed (first run of a NEW version) ────────────────────────────
  // The Tauri shell eval()s `showWhatsChanged('x.y.z', '<release notes>')` into
  // the main window on the first launch after an update — the launch check now
  // installs OTA updates by itself, so this is the only place the user learns
  // what actually changed. It never fires on a fresh install (the shell has no
  // previous version on record) and the shell records "seen" as soon as this
  // call lands, so it does not come back after a dismissal.
  //
  // Same markup style as ensureSigninModal below (.modal-* from style.css) so
  // it looks like the rest of the app. No inline handlers — CSP-safe.
  //
  // ⚠ THE DISMISSAL IS PERSISTED, unlike showUpdateNotice's in-memory flag. The
  // shell has to keep re-delivering this call for a while (the main window
  // re-navigates to :8765 the moment the sidecar comes up, which destroys any
  // JS state and would swallow a single early attempt), and a memory-only guard
  // resets on exactly that navigation — so a dismissed panel would come back.
  // localStorage is per-origin and every app page is served from :8765.
  const WC_KEY = 'scry-whats-changed-dismissed';
  let whatsChangedFor = null;
  window.showWhatsChanged = function (version, notes) {
    if (version === whatsChangedFor) return;
    try {
      if (localStorage.getItem(WC_KEY) === version) return;
    } catch (e) { /* private mode / storage disabled — show it anyway */ }
    whatsChangedFor = version;
    document.getElementById('whats-changed-modal')?.remove();

    const modal = document.createElement('div');
    modal.id = 'whats-changed-modal';
    modal.className = 'modal-backdrop';

    const container = document.createElement('div');
    container.className = 'modal-container';

    const header = document.createElement('div');
    header.className = 'modal-header';
    const title = document.createElement('span');
    title.className = 'modal-title';
    title.textContent = "What's changed in v" + version;
    const closeBtn = document.createElement('button');
    closeBtn.className = 'btn btn-ghost btn-sm';
    closeBtn.textContent = '✕';
    header.appendChild(title);
    header.appendChild(closeBtn);

    const body = document.createElement('div');
    body.className = 'modal-body';
    // md_render.js ships only on /decks, so this is a progressive upgrade:
    // rendered markdown when it is loaded, readable plain text otherwise.
    // Both paths build nodes — the notes are never assigned as innerHTML.
    if (typeof window.renderMarkdown === 'function') {
      body.appendChild(window.renderMarkdown(notes, document));
    } else {
      const pre = document.createElement('div');
      pre.style.cssText = 'white-space:pre-wrap;font-size:13px;line-height:1.55;';
      pre.textContent = String(notes == null ? '' : notes);
      body.appendChild(pre);
    }

    const footer = document.createElement('div');
    footer.className = 'modal-footer';
    const okBtn = document.createElement('button');
    okBtn.className = 'btn btn-primary';
    okBtn.textContent = 'Got it';
    footer.appendChild(okBtn);

    container.appendChild(header);
    container.appendChild(body);
    container.appendChild(footer);
    modal.appendChild(container);

    function dismiss() {
      try { localStorage.setItem(WC_KEY, version); } catch (e) { /* best effort */ }
      document.removeEventListener('keydown', onKey);
      modal.remove();
    }
    function onKey(e) {
      if (e.key === 'Escape') dismiss();
    }
    closeBtn.addEventListener('click', dismiss);
    okBtn.addEventListener('click', dismiss);
    modal.addEventListener('click', (e) => {
      if (e.target === modal) dismiss();
    });
    document.addEventListener('keydown', onKey);

    document.body.appendChild(modal);
    modal.classList.add('open');
  };

  window.ScryglassShell = {
    user: { email: null, is_anon: true, alpha: false, created_at: null },
    syncing: false,

    init: function() {
      this.bindAccountControl();
      this.fetchAccountStatus();
      this.highlightActiveNav();
    },

    highlightActiveNav: function() {
      const path = window.location.pathname;
      document.querySelectorAll('.main-nav .nav-link').forEach(link => {
        const href = link.getAttribute('href');
        if (href === path || (path === '/' && href === '/index.html') || (href === '/' && path === '/index.html')) {
          link.classList.add('active');
        } else if (href !== '/' && path.startsWith(href)) {
          link.classList.add('active');
        } else {
          link.classList.remove('active');
        }
      });
    },

    bindAccountControl: function() {
      const pill = document.getElementById('account-pill');
      const dropdown = document.getElementById('account-dropdown');

      if (pill && dropdown) {
        pill.addEventListener('click', (e) => {
          e.stopPropagation();
          dropdown.classList.toggle('open');
        });

        document.addEventListener('click', (e) => {
          if (!dropdown.contains(e.target) && !pill.contains(e.target)) {
            dropdown.classList.remove('open');
          }
        });
      }

      // Action buttons inside dropdown
      const btnSync = document.getElementById('btn-cloud-sync');
      if (btnSync) {
        btnSync.addEventListener('click', () => this.triggerSync());
      }

      const btnSignout = document.getElementById('btn-cloud-signout');
      if (btnSignout) {
        btnSignout.addEventListener('click', () => this.signOut());
      }

      const btnSignin = document.getElementById('btn-cloud-signin');
      if (btnSignin) {
        btnSignin.addEventListener('click', () => this.openSigninModal());
      }
    },

    fetchAccountStatus: async function() {
      try {
        const res = await fetch('/api/manage/cloud-me');
        if (res.ok) {
          const data = await res.json();
          if (data.status === 'ok' && data.body) {
            this.user = data.body;
            this.renderAccountUI();
          }
        }
      } catch (e) {
        console.warn('Scryglass: Cloud status check skipped or failed', e);
      }
    },

    renderAccountUI: function() {
      const pill = document.getElementById('account-pill');
      if (!pill) return;

      if (this.user.is_anon) {
        pill.innerHTML = `
          <span class="user-avatar" style="background:#5b6ccf">?</span>
          <div style="display:flex; flex-direction:column; gap:0px;">
            <span class="account-email">Anonymous User</span>
            <span class="account-anon-hint">syncing anonymously</span>
          </div>
          <button id="btn-cloud-signin" class="btn btn-primary btn-sm" style="margin-left:6px;">Sign in</button>
        `;
        document.getElementById('btn-cloud-signin')?.addEventListener('click', (e) => {
          e.stopPropagation();
          this.openSigninModal();
        });
      } else {
        const initial = (this.user.email || 'U').charAt(0).toUpperCase();
        pill.innerHTML = `
          <span class="user-avatar" style="background:#58c98b">${initial}</span>
          <div style="display:flex; flex-direction:column; gap:0px;">
            <span class="account-email">${this.user.email}</span>
            <span class="account-anon-hint">${this.user.alpha ? 'Alpha Member' : 'Cloud Connected'}</span>
          </div>
          <span style="font-size:10px; color:var(--muted); margin-left:4px;">▼</span>
        `;
      }

      // Update dropdown details
      const dropdownEmail = document.getElementById('dropdown-email-display');
      if (dropdownEmail) {
        dropdownEmail.textContent = this.user.is_anon ? 'Anonymous Device Account' : this.user.email;
      }
      const alphaBadge = document.getElementById('dropdown-alpha-badge');
      if (alphaBadge) {
        alphaBadge.style.display = this.user.alpha ? 'inline-block' : 'none';
      }
    },

    // Ensure the sign-in modal exists on this page (only index.html ships it in
    // markup; the shell injects it on the dashboards so sign-in feedback is
    // consistent everywhere). Styled by style.css (.modal-backdrop/.modal-*).
    ensureSigninModal: function() {
      let modal = document.getElementById('signin-modal');
      if (modal) return modal;
      modal = document.createElement('div');
      modal.id = 'signin-modal';
      modal.className = 'modal-backdrop';
      modal.innerHTML = `
        <div class="modal-container">
          <div class="modal-header">
            <span class="modal-title">Sign in to Scryglass Cloud</span>
            <button class="btn btn-ghost btn-sm" id="signin-modal-close">✕</button>
          </div>
          <div class="modal-body" style="text-align:center; padding:24px;">
            <div style="font-size:28px; margin-bottom:12px;">☁️</div>
            <h3 style="margin-bottom:8px;">Completing sign-in in your browser…</h3>
            <p style="font-size:12px; color:var(--muted); margin-bottom:16px;" id="signin-status-msg">
              A browser window opened. Scryglass will link this device to your account once you approve.
            </p>
            <div class="progress-bar"><div class="progress-fill" style="width:60%;"></div></div>
          </div>
        </div>`;
      document.body.appendChild(modal);
      modal.querySelector('#signin-modal-close')?.addEventListener('click', () => modal.classList.remove('open'));
      return modal;
    },

    openSigninModal: function() {
      const modal = this.ensureSigninModal();
      if (modal) modal.classList.add('open');
      this.startSigninFlow();
    },

    startSigninFlow: async function() {
      const statusText = document.getElementById('signin-status-msg');
      if (statusText) statusText.textContent = 'Connecting to Scryglass Auth in browser...';

      try {
        // Blocks up to ~3 min while the human completes the browser flow.
        const res = await fetch('/api/manage/cloud-signin', { method: 'POST' });
        const data = await res.json().catch(() => ({}));
        if (res.ok && data.status === 'ok') {
          if (statusText) statusText.textContent = 'Sign-in complete! Updating session...';
          setTimeout(() => {
            document.getElementById('signin-modal')?.classList.remove('open');
            this.fetchAccountStatus();
          }, 1200);
        } else if (statusText) {
          statusText.textContent = data.error || 'Sign-in did not complete. Please try again.';
        }
      } catch (e) {
        if (statusText) statusText.textContent = 'Sign-in error. Please try again.';
      }
    },

    triggerSync: async function() {
      const syncText = document.getElementById('sync-time-text');
      if (syncText) syncText.textContent = 'Syncing now...';

      try {
        await fetch('/api/manage/cloud-sync', { method: 'POST' });
        setTimeout(() => {
          if (syncText) syncText.textContent = 'Synced just now';
        }, 1000);
      } catch (e) {
        if (syncText) syncText.textContent = 'Sync failed';
      }
    },

    signOut: async function() {
      // Forget the account token on this device (host clears it locally and
      // reverts to anonymous — the cloud account is untouched). Then re-read the
      // real status so the widget reflects the freshly-provisioned anon account.
      document.getElementById('account-dropdown')?.classList.remove('open');
      try {
        await fetch('/api/manage/cloud-signout', { method: 'POST' });
      } catch (e) {
        console.error('Sign-out error', e);
      }
      this.fetchAccountStatus();
    }
  };

  document.addEventListener('DOMContentLoaded', () => window.ScryglassShell.init());
})();
