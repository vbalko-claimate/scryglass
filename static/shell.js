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
  // The update dialog (banner and About): check → download with a progress bar → "Restart now?".
  // Drives the shell's `update_start` / `update_restart` commands; the shell refuses to download
  // or restart during a live match and keeps a downloaded update for "Later".
  window.scryglassUpdate = async function () {
    if (document.getElementById('shell-update-dialog')) return;
    const css = (el, c) => { el.style.cssText = c; return el; };
    const wrap = css(document.createElement('div'),
      'position:fixed;inset:0;z-index:10000;display:flex;align-items:center;justify-content:center;' +
      'background:rgba(0,0,0,0.55);');
    wrap.id = 'shell-update-dialog';
    const box = css(document.createElement('div'),
      'background:#1c1f2b;color:#e8eaf2;border-radius:10px;padding:18px 20px;width:min(420px,90vw);' +
      'font:14px system-ui,sans-serif;box-shadow:0 10px 40px rgba(0,0,0,0.5);');
    const title = css(document.createElement('div'), 'font-weight:700;font-size:16px;margin-bottom:10px;');
    const msg = css(document.createElement('div'), 'margin-bottom:12px;line-height:1.4;');
    const track = css(document.createElement('div'),
      'height:8px;border-radius:4px;background:rgba(255,255,255,0.12);overflow:hidden;margin-bottom:12px;');
    const fill = css(document.createElement('div'), 'height:100%;width:0;background:#ffd166;');
    track.appendChild(fill);
    const notes = css(document.createElement('div'),
      'max-height:140px;overflow:auto;font-size:12px;opacity:0.8;white-space:pre-wrap;margin-bottom:12px;');
    const row = css(document.createElement('div'), 'display:flex;gap:8px;justify-content:flex-end;');
    const btn = (label, primary) => {
      const b = document.createElement('button');
      b.textContent = label;
      css(b, 'border:none;border-radius:5px;padding:6px 14px;cursor:pointer;font-weight:600;' +
        (primary ? 'background:#ffd166;color:#0f1117;' : 'background:rgba(255,255,255,0.14);color:#e8eaf2;'));
      return b;
    };
    box.append(title, msg, track, notes, row);
    wrap.appendChild(box);
    document.body.appendChild(wrap);
    const close = () => { unlisten(); wrap.remove(); };
    let unlisten = () => {};
    const show = (t, m, buttons, withBar) => {
      title.textContent = t;
      msg.textContent = m;
      track.style.display = withBar ? '' : 'none';
      row.replaceChildren(...buttons);
    };
    const closeBtn = () => { const b = btn('Close', false); b.addEventListener('click', close); return b; };
    const tauri = window.__TAURI__;
    if (!tauri || !tauri.core) {
      show('Update', 'Updates can only be started from the Scryglass app.', [closeBtn()], false);
      return;
    }
    show('Checking for updates…', 'Looking for a newer version.', [], true);
    fill.style.width = '0';
    try {
      unlisten = await tauri.event.listen('update-progress', (e) => {
        const { downloaded, total } = e.payload || {};
        if (total) {
          fill.style.width = Math.min(100, (100 * downloaded) / total).toFixed(1) + '%';
          msg.textContent = 'Downloading… ' + Math.floor((100 * downloaded) / total) + '% (' +
            (downloaded / 1048576).toFixed(1) + ' of ' + (total / 1048576).toFixed(1) + ' MB)';
        } else {
          msg.textContent = 'Downloading… ' + (downloaded / 1048576).toFixed(1) + ' MB';
        }
        title.textContent = 'Downloading update';
      });
    } catch (_) { /* progress is optional; the download still runs */ }
    let out;
    try {
      out = await tauri.core.invoke('update_start');
    } catch (e) {
      show('Update failed', String((e && e.message) || e), [closeBtn()], false);
      return;
    }
    if (out.status === 'uptodate') {
      show('You are up to date', 'This is the latest version of Scryglass.', [closeBtn()], false);
    } else if (out.status === 'match') {
      show('A match is in progress',
        'Scryglass v' + out.version + ' is available. It will not be installed during a match — ' +
        'try again when it ends.', [closeBtn()], false);
    } else {
      fill.style.width = '100%';
      if (out.notes) notes.textContent = "What's new:\n" + out.notes;
      const later = btn('Later', false);
      later.addEventListener('click', close);
      const go = btn('Restart now', true);
      go.addEventListener('click', async () => {
        show('Installing', 'Installing v' + out.version + ' — Scryglass will restart.', [], true);
        notes.style.display = 'none';
        try {
          await tauri.core.invoke('update_restart');
        } catch (e) {
          show('Update not installed', String((e && e.message) || e), [closeBtn()], false);
        }
      });
      show('Update downloaded', 'Scryglass v' + out.version + ' is ready. Restart now to finish installing?',
        [later, go], true);
    }
  };
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
      const btnCss =
        'border:none;border-radius:4px;padding:2px 10px;cursor:pointer;' +
        'background:rgba(15,17,23,0.15);color:#0f1117;font-weight:600;';
      const install = document.createElement('button');
      install.id = 'shell-update-install';
      install.textContent = 'Install update';
      install.style.cssText = btnCss + 'background:#0f1117;color:#ffd166;';
      install.addEventListener('click', () => window.scryglassUpdate());
      const btn = document.createElement('button');
      btn.textContent = 'Dismiss';
      btn.style.cssText = btnCss;
      btn.addEventListener('click', () => {
        dismissedUpdate = version;
        bar.remove();
      });
      bar.appendChild(txt);
      bar.appendChild(install);
      bar.appendChild(btn);
      document.body.appendChild(bar);
    }
    document.getElementById('shell-update-notice-text').textContent =
      'Update v' + version + ' is ready.';
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
      this.initSyncStatus();
    },

    // ── Sync status chip (#22 wave 3B) ──────────────────────────────────────
    // Several pages ship a hardcoded "Synced 2m ago" in their markup
    // (#sync-time-text) that never updated — pure decoration. There is no
    // server-side timestamp this client can read to compute a real age:
    // GET /api/manage/cloud-me was live-verified (2026-09-03, localhost:8765)
    // to return only {alpha, created_at, email, has_password, is_admin,
    // is_anon, passkey_count} — no last-sync field of any kind — and its
    // upstream /me profile carries none either. `POST /api/manage/cloud-sync`
    // returns a SyncReport (uploaded/bundle_version/error/…) but that report
    // is never persisted anywhere the client can re-read later, so it can only
    // inform the moment of the click itself.
    //
    // So the only truthful age this widget can ever show is computed from a
    // timestamp THIS BROWSER recorded itself, at the moment a manual "Sync
    // now" click actually succeeded (persisted in localStorage so it survives
    // reloads). Absent that, it falls back to "Sync: on/off" from cloud-me's
    // enabled/disabled status — never a fabricated relative age.
    //
    // Follow-up for the server: /api/manage/cloud-me (or /me) would need a
    // real `last_sync_at` / `last_ingest_at` field for a true cross-session,
    // background-sync-aware age to be possible.
    SYNC_KEY: 'scry-last-manual-sync-at',
    syncEnabled: null,
    lastManualSyncAt: null,

    initSyncStatus: function() {
      try {
        const saved = localStorage.getItem(this.SYNC_KEY);
        this.lastManualSyncAt = saved ? parseInt(saved, 10) : null;
      } catch (e) { this.lastManualSyncAt = null; }
      this.renderSyncStatus();
      this.pollSyncStatus();
      setInterval(() => this.pollSyncStatus(), 60000);
    },

    pollSyncStatus: async function() {
      try {
        const res = await fetch('/api/manage/cloud-me');
        const data = await res.json().catch(() => ({}));
        this.syncEnabled = data.status === 'ok' ? true : (data.status === 'disabled' ? false : null);
      } catch (e) {
        this.syncEnabled = null;
      }
      this.renderSyncStatus();
    },

    formatAge: function(ms) {
      const s = Math.floor(ms / 1000);
      if (s < 60) return 'just now';
      const m = Math.floor(s / 60);
      if (m < 60) return m + 'm ago';
      const h = Math.floor(m / 60);
      if (h < 24) return h + 'h ago';
      return Math.floor(h / 24) + 'd ago';
    },

    renderSyncStatus: function() {
      const el = document.getElementById('sync-time-text');
      if (!el) return;
      if (this.lastManualSyncAt) {
        el.textContent = 'Synced ' + this.formatAge(Date.now() - this.lastManualSyncAt);
      } else if (this.syncEnabled === false) {
        el.textContent = 'Sync: off';
      } else if (this.syncEnabled === true) {
        el.textContent = 'Sync: on';
      } else {
        el.textContent = '—';
      }
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
        const res = await fetch('/api/manage/cloud-sync', { method: 'POST' });
        const data = await res.json().catch(() => ({}));
        // Only stamp success on an ACTUAL ok report (manage_cloud_sync returns
        // status:"error" for a misconfigured/failed sync with HTTP 200) — a
        // fake "Synced" on a failed cycle is exactly the decorative bug this
        // replaces.
        if (res.ok && data.status === 'ok') {
          this.lastManualSyncAt = Date.now();
          try { localStorage.setItem(this.SYNC_KEY, String(this.lastManualSyncAt)); } catch (e) { /* private mode */ }
          this.renderSyncStatus();
        } else if (syncText) {
          syncText.textContent = 'Sync failed';
        }
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
