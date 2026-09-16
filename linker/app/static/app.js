    function apiFetch(url, options = {}) {
      if (!['GET', 'HEAD', 'OPTIONS'].includes((options.method || 'GET').toUpperCase())) {
        options.headers = {
          ...options.headers,
          'Content-Type': 'application/json',
          'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content,
        };
      }
      return fetch(url, options);
    }

    async function refresh() {
      const res = await apiFetch('/api/status');
      const data = await res.json();
      document.getElementById('last-run').textContent = data.last_run || 'never';
      document.getElementById('running-indicator').style.display = data.running ? 'inline' : 'none';
      document.getElementById('log').textContent = data.log.join('\n');
      document.getElementById('auto-update-toggle').checked = data.auto_update;
      document.getElementById('auto-update-label').textContent = data.auto_update ? 'Auto-update: on' : 'Auto-update: off';

      const rows = document.getElementById('service-rows');
      rows.innerHTML = '';
      for (const [key, s] of Object.entries(data.services)) {
        const linked = Object.entries(s.linked || {})
          .map(([t, ok]) => `<span class="badge ${ok ? 'ok' : 'fail'}">${t}</span>`)
          .join(' ') || '<span class="muted">-</span>';
        rows.innerHTML += `
          <tr>
            <td>${s.label}</td>
            <td><span class="dot ${s.reachable ? 'up' : 'down'}"></span> ${s.reachable ? 'running' : 'unreachable'}
              ${s.error ? `<span class="err">(${s.error})</span>` : ''}</td>
            <td>${linked}</td>
            <td><a class="open" href="${s.url}" target="_blank" rel="noopener noreferrer">Open ↗</a></td>
          </tr>`;
      }
    }

    async function loadKeyMetadata() {
      const res = await apiFetch('/api/keys');
      const data = await res.json();
      renderKeys(data);
    }

    function renderKeys(data) {
      const rows = document.getElementById('key-rows');
      rows.innerHTML = '';
      for (const [key, info] of Object.entries(data)) {
        const label = key.charAt(0).toUpperCase() + key.slice(1);
        const keyText = info.api_key || (info.key_available ? '(hidden)' : '(not available yet)');
        rows.innerHTML += `
          <tr>
            <td>${label}</td>
            <td><code>${info.host}</code></td>
            <td><code>${info.port}</code></td>
            <td><code>${keyText}</code></td>
            <td>${info.api_key ? `<button class="copy-btn" data-copy="${keyText}">Copy key</button>` : ''}</td>
          </tr>`;
      }
      attachCopyButtons(rows);
    }

    document.getElementById('reveal-keys-btn').addEventListener('click', () => {
      document.getElementById('key-reveal-form').hidden = false;
      document.getElementById('key-password').focus();
    });

    document.getElementById('key-reveal-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const input = document.getElementById('key-password');
      const result = document.getElementById('key-reveal-result');
      const request = apiFetch('/api/keys/reveal', {
        method: 'POST',
        body: JSON.stringify({ password: input.value }),
      });
      input.value = '';
      try {
        const response = await request;
        if (!response.ok) {
          result.textContent = 'Reveal failed. Check your password or reload the page.';
          return;
        }
        renderKeys(await response.json());
        document.getElementById('key-reveal-form').hidden = true;
        result.textContent = 'Keys revealed for this page only.';
      } catch {
        result.textContent = 'Reveal failed. Please try again.';
      }
    });

    // navigator.clipboard needs a "secure context" (HTTPS, or the page
    // itself being served from localhost) - visiting this dashboard as
    // plain http://<server-ip>:5050 doesn't count, so it's often simply
    // undefined there. Fall back to the old execCommand approach (works
    // over plain HTTP) instead of silently doing nothing.
    async function copyText(text) {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return;
      }
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      if (!ok) throw new Error('copy command was not allowed by the browser');
    }

    function attachCopyButtons(container) {
      container.querySelectorAll('.copy-btn').forEach((btn) => {
        btn.addEventListener('click', async () => {
          const original = btn.textContent;
          try {
            await copyText(btn.dataset.copy);
            btn.textContent = 'Copied!';
          } catch (e) {
            btn.textContent = 'Copy failed';
            console.error('Copy failed:', e);
          }
          setTimeout(() => { btn.textContent = original; }, 1500);
        });
      });
    }

    document.getElementById('scan-drives-btn').addEventListener('click', async () => {
      const rows = document.getElementById('drive-rows');
      rows.innerHTML = '<tr><td colspan="4" class="muted">Scanning...</td></tr>';
      const res = await apiFetch('/api/drives');
      const data = await res.json();
      if (!data.drives.length) {
        rows.innerHTML = '<tr><td colspan="4" class="muted">No drives found under /Volumes, /mnt or /media. ' +
          'On macOS, check Docker Desktop -> Settings -> Resources -> File Sharing includes that path.</td></tr>';
        return;
      }
      rows.innerHTML = '';
      for (const d of data.drives) {
        rows.innerHTML += `
          <tr>
            <td>${d.label || d.host_path}<br><code class="muted small">${d.host_path}</code></td>
            <td>${d.free_human}</td>
            <td>${d.total_human}</td>
            <td>
              <button class="copy-btn" data-copy="${d.host_path}">Copy path</button>
              <button class="use-drive-btn" data-path="${d.host_path}">Use this</button>
            </td>
          </tr>`;
      }
      attachCopyButtons(rows);
      rows.querySelectorAll('.use-drive-btn').forEach((btn) => {
        btn.addEventListener('click', async () => {
          btn.disabled = true;
          btn.textContent = 'Applying...';
          const box = document.getElementById('use-drive-result');
          box.hidden = false;
          box.innerHTML = '<p class="muted">Writing .env and recreating Sonarr, Radarr, Plex and qBittorrent with the new paths - this can take a bit...</p>';
          const res = await apiFetch('/api/drives/use', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: btn.dataset.path }),
          });
          const result = await res.json();
          if (result.ok && result.applied) {
            box.innerHTML = `
              <p><strong>Done - applied automatically.</strong> Now storing media on:</p>
              <ul>
                <li>Movies: <code>${result.paths.MEDIA_MOVIES_PATH}</code></li>
                <li>TV: <code>${result.paths.MEDIA_TV_PATH}</code></li>
                <li>Downloads: <code>${result.paths.DOWNLOADS_PATH}</code></li>
              </ul>
              <p class="muted small">Existing files already in the old location were not moved.</p>`;
            btn.textContent = 'Done';
            setTimeout(refresh, 3000);
          } else if (result.ok && !result.applied) {
            box.innerHTML = `<p class="err">.env was updated, but applying it failed: ${result.apply_output}</p>
              <p class="muted small">Fix the issue above, then run <code>docker compose up -d</code> on the host yourself to apply the already-saved paths.</p>`;
            btn.disabled = false;
            btn.textContent = 'Use this';
          } else {
            box.innerHTML = `<p class="err">Failed: ${result.error}</p>`;
            btn.disabled = false;
            btn.textContent = 'Use this';
          }
        });
      });
    });

    document.getElementById('plex-claim-btn').addEventListener('click', async () => {
      const input = document.getElementById('plex-claim-input');
      const btn = document.getElementById('plex-claim-btn');
      const box = document.getElementById('plex-claim-result');
      const token = input.value.trim();
      if (!token) return;
      btn.disabled = true;
      btn.textContent = 'Applying...';
      box.hidden = false;
      box.innerHTML = '<p class="muted">Saving token and recreating Plex...</p>';
      const res = await apiFetch('/api/plex-claim', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token }),
      });
      const result = await res.json();
      if (result.ok && result.applied) {
        box.innerHTML = '<p><strong>Done.</strong> Plex has been recreated and should be claimed to your account now.</p>';
        input.value = '';
        setTimeout(refresh, 3000);
      } else if (result.ok && !result.applied) {
        box.innerHTML = `<p class="err">Token was saved, but applying it failed: ${result.apply_output}</p>
          <p class="muted small">If the token expired (5 min limit), get a fresh one and try again.</p>`;
      } else {
        box.innerHTML = `<p class="err">Failed: ${result.error}</p>`;
      }
      btn.disabled = false;
      btn.textContent = 'Save token';
    });

    function applyMonthlyModeVisibility() {
      const mode = document.getElementById('cleanup-monthly-mode').value;
      document.getElementById('cleanup-monthly-day-wrap').style.display = mode === 'day_of_month' ? 'inline-block' : 'none';
      document.getElementById('cleanup-monthly-nth-wrap').style.display = mode === 'nth_weekday' ? 'inline-block' : 'none';
      document.getElementById('cleanup-monthly-weekday-wrap').style.display = mode === 'nth_weekday' ? 'inline-block' : 'none';
    }

    function applyCleanupVisibility(freq) {
      document.getElementById('cleanup-weekly-row').style.display = freq === 'weekly' ? 'flex' : 'none';
      document.getElementById('cleanup-monthly-row').style.display = freq === 'monthly' ? 'flex' : 'none';
      applyMonthlyModeVisibility();
    }

    function applyCleanupSettings(c) {
      document.getElementById('cleanup-enabled-toggle').checked = c.enabled;
      document.getElementById('cleanup-enabled-label').textContent = c.enabled ? 'Cleanup: on' : 'Cleanup: off';
      document.getElementById('cleanup-frequency').value = c.frequency;
      document.getElementById('cleanup-time').value = c.time;
      document.getElementById('cleanup-weekly-day').value = c.weekly_day;
      document.getElementById('cleanup-monthly-mode').value = c.monthly_mode;
      document.getElementById('cleanup-monthly-day').value = c.monthly_day;
      document.getElementById('cleanup-monthly-nth').value = c.monthly_nth;
      document.getElementById('cleanup-monthly-weekday').value = c.monthly_weekday;
      document.getElementById('cleanup-last-run').textContent = c.last_run || 'never';
      document.getElementById('cleanup-next-run').textContent = c.next_run || '-';
      applyCleanupVisibility(c.frequency);
    }

    async function loadCleanupSettings() {
      const res = await apiFetch('/api/cleanup/settings');
      const data = await res.json();
      applyCleanupSettings(data);
    }

    document.getElementById('cleanup-frequency').addEventListener('change', (e) => applyCleanupVisibility(e.target.value));
    document.getElementById('cleanup-monthly-mode').addEventListener('change', applyMonthlyModeVisibility);

    async function saveCleanupSettings(patch) {
      const res = await apiFetch('/api/cleanup/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch),
      });
      const result = await res.json();
      applyCleanupSettings(result);
      return result;
    }

    document.getElementById('cleanup-enabled-toggle').addEventListener('change', async (e) => {
      await saveCleanupSettings({ enabled: e.target.checked });
    });

    document.getElementById('cleanup-save-btn').addEventListener('click', async () => {
      const btn = document.getElementById('cleanup-save-btn');
      btn.disabled = true;
      btn.textContent = 'Saving...';
      await saveCleanupSettings({
        enabled: document.getElementById('cleanup-enabled-toggle').checked,
        frequency: document.getElementById('cleanup-frequency').value,
        time: document.getElementById('cleanup-time').value,
        weekly_day: parseInt(document.getElementById('cleanup-weekly-day').value, 10),
        monthly_mode: document.getElementById('cleanup-monthly-mode').value,
        monthly_day: parseInt(document.getElementById('cleanup-monthly-day').value, 10),
        monthly_nth: parseInt(document.getElementById('cleanup-monthly-nth').value, 10),
        monthly_weekday: parseInt(document.getElementById('cleanup-monthly-weekday').value, 10),
      });
      btn.disabled = false;
      btn.textContent = 'Save schedule';
    });

    document.getElementById('cleanup-run-now-btn').addEventListener('click', async () => {
      const btn = document.getElementById('cleanup-run-now-btn');
      const box = document.getElementById('cleanup-result');
      btn.disabled = true;
      btn.textContent = 'Running...';
      box.hidden = false;
      box.innerHTML = '<p class="muted">Deleting everything in the downloads folder...</p>';
      const res = await apiFetch('/api/cleanup/run-now', { method: 'POST' });
      const result = await res.json();
      box.innerHTML = result.ok
        ? `<p><strong>Done.</strong> ${result.message}</p>`
        : `<p class="err">Failed: ${result.message}</p>`;
      btn.disabled = false;
      btn.textContent = 'Run now';
      loadCleanupSettings();
      refresh();
    });

    document.getElementById('relink-btn').addEventListener('click', async () => {
      await apiFetch('/api/relink', { method: 'POST' });
      refresh();
      setTimeout(loadKeyMetadata, 2000);
    });

    document.getElementById('auto-update-toggle').addEventListener('change', async (e) => {
      await apiFetch('/api/settings/auto-update', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: e.target.checked }),
      });
      refresh();
    });

    document.getElementById('update-now-btn').addEventListener('click', async () => {
      await apiFetch('/api/update-now', { method: 'POST' });
      refresh();
    });

    refresh();
    setInterval(refresh, 4000);
    loadKeyMetadata();
    loadCleanupSettings();
