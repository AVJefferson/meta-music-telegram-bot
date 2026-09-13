(() => {
  const tg = window.Telegram && window.Telegram.WebApp;
  const BOT = "@MetaMusicProBot";
  const mainEl = document.getElementById("main");
  const heading = document.getElementById("heading");
  const banner = document.getElementById("banner");
  const tabs = document.getElementById("tabs");
  const TITLES = {
    home: "Home",
    review: "Review",
    suggest: "Suggest",
    settings: "Settings",
    login: "Connect Drive",
  };

  let me = null;
  let page = "home";
  let pollTimer = null;
  let polling = false;
  let autoLoginDone = false;
  let mainHandler = null;
  let suggestQuery = new URLSearchParams(location.search).get("q") || "";

  function esc(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function pageFromPath() {
    const parts = location.pathname.replace(/\/+$/, "").split("/");
    const last = parts.pop() || "home";
    if (last === "app" || last === "home" || last === "") return "home";
    if (["login", "settings", "review", "suggest"].includes(last)) return last;
    return "home";
  }

  function applyTheme() {
    if (!tg) return;
    tg.ready();
    if (tg.expand) tg.expand();
    const params = tg.themeParams || {};
    if (params.bg_color) {
      if (tg.setBackgroundColor) tg.setBackgroundColor(params.bg_color);
      if (tg.setHeaderColor) tg.setHeaderColor("bg_color");
    }
    document.documentElement.classList.toggle("tg-dark", tg.colorScheme === "dark");
    document.documentElement.classList.toggle("tg-light", tg.colorScheme !== "dark");
    document.documentElement.style.colorScheme = tg.colorScheme === "dark" ? "dark" : "light";
  }

  function haptic(kind) {
    const h = tg && tg.HapticFeedback;
    if (!h) return;
    if (kind === "success" && h.notificationOccurred) h.notificationOccurred("success");
    else if (kind === "error" && h.notificationOccurred) h.notificationOccurred("error");
    else if (h.impactOccurred) h.impactOccurred("light");
  }

  function openExternal(url) {
    if (!url) return;
    if (tg && tg.openLink) tg.openLink(url, { try_instant_view: false });
    else location.href = url;
  }

  function setMainButton(text, fn) {
    if (!tg || !tg.MainButton) return;
    if (mainHandler) tg.MainButton.offClick(mainHandler);
    mainHandler = fn;
    tg.MainButton.setText(text);
    tg.MainButton.onClick(mainHandler);
    tg.MainButton.show();
  }

  function hideMainButton() {
    if (!tg || !tg.MainButton) return;
    if (mainHandler) tg.MainButton.offClick(mainHandler);
    mainHandler = null;
    tg.MainButton.hide();
  }

  function setWaiting(on) {
    document.body.classList.toggle("waiting", on);
    if (tg) {
      if (on && tg.enableClosingConfirmation) tg.enableClosingConfirmation();
      if (!on && tg.disableClosingConfirmation) tg.disableClosingConfirmation();
    }
  }

  function showTabs() {
    tabs.hidden = false;
    const active = page === "login" ? "settings" : page;
    tabs.querySelectorAll("button").forEach((btn) => {
      btn.classList.toggle("is-on", btn.dataset.page === active);
    });
  }

  function hideTabs() {
    tabs.hidden = true;
  }

  function showBanner(err) {
    const text = (err && (err.error || err.message)) || "Failed";
    banner.hidden = false;
    banner.textContent = text;
  }

  function clearBanner() {
    banner.hidden = true;
    banner.textContent = "";
  }

  function skeletons(n) {
    return Array.from({ length: n }, () => '<div class="skel"></div>').join("");
  }

  function formatSince(iso) {
    if (!iso) return "—";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return String(iso);
    return date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  }

  function destLabel(dest) {
    if (dest === "library") return "library";
    if (dest === "review") return "review";
    return "Telegram only";
  }

  function trackBits(track) {
    const stem = String(track.file_name || track.relative_path || "track").replace(/\.[^.]+$/, "");
    return {
      artist: track.artist || stem,
      title: track.title || stem,
      album: track.album || "",
    };
  }

  async function api(path, opts) {
    const headers = Object.assign(
      { "X-Telegram-Init-Data": tg ? tg.initData : "" },
      (opts && opts.headers) || {},
    );
    const res = await fetch(path, Object.assign({}, opts, { headers }));
    let data;
    try {
      data = await res.json();
    } catch {
      throw { ok: false, error: "Failed" };
    }
    if (!data.ok) throw data;
    return data;
  }

  function stopPoll() {
    polling = false;
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  async function pingMe() {
    const next = await api("/api/me");
    me = next;
    if (next.logged_in && polling) {
      stopPoll();
      setWaiting(false);
      hideMainButton();
      haptic("success");
      page = "settings";
      history.replaceState({ page }, "", "/app/settings");
      heading.textContent = TITLES.settings;
      renderSettings();
      showTabs();
    }
    return next;
  }

  function startPoll() {
    stopPoll();
    polling = true;
    const deadline = Date.now() + 120000;
    pollTimer = setInterval(async () => {
      if (Date.now() > deadline) {
        stopPoll();
        renderLoginTimeout();
        return;
      }
      try {
        await pingMe();
      } catch {
        /* keep waiting */
      }
    }, 2000);
  }

  async function startConnect() {
    setWaiting(true);
    hideTabs();
    renderLoginWaiting();
    try {
      const data = await api("/api/login");
      openExternal(data.url);
      startPoll();
      setMainButton("Open Google again", () => {
        startConnect();
      });
    } catch (err) {
      haptic("error");
      showBanner(err);
      setWaiting(false);
      renderLoginIdle();
      showTabs();
    }
  }

  function cancelConnect() {
    stopPoll();
    setWaiting(false);
    hideMainButton();
    page = "settings";
    history.replaceState({ page }, "", "/app/settings");
    heading.textContent = TITLES.settings;
    renderSettings();
    showTabs();
  }

  function confirmRemove() {
    const text = "Remove this track from your review?";
    if (tg && tg.showConfirm) {
      return new Promise((resolve) => tg.showConfirm(text, resolve));
    }
    return Promise.resolve(window.confirm(text));
  }

  function confirmUnlink() {
    const text = "Unlink Google Drive? The bot will forget this login.";
    if (tg && tg.showConfirm) {
      return new Promise((resolve) => tg.showConfirm(text, resolve));
    }
    return Promise.resolve(window.confirm(text));
  }

  function destSeg(current) {
    return `<div class="seg" role="radiogroup" aria-label="Default save">
      ${["library", "review", "none"]
        .map(
          (dest) =>
            `<button type="button" class="seg-btn${current === dest ? " is-on" : ""}" data-dest="${dest}">${
              dest === "none" ? "None" : dest[0].toUpperCase() + dest.slice(1)
            }</button>`,
        )
        .join("")}
    </div>`;
  }

  function renderHome() {
    const driveOn = Boolean(me && me.logged_in);
    const email = (me && me.google_email) || "Drive on";
    mainEl.innerHTML = `
      <div class="card">
        <div class="row-between">
          <h2>Google Drive</h2>
          <span class="pill${driveOn ? " on" : ""}">${driveOn ? "Connected" : "Off"}</span>
        </div>
        <p class="muted">${driveOn ? esc(email) : "Tagging still works in Telegram without Drive."}</p>
        ${driveOn ? "" : `<button type="button" class="btn btn-primary mt" data-act="connect">Connect Google Drive</button>`}
      </div>
      <div class="metrics">
        <div class="metric"><strong>${esc(me.library_count || 0)}</strong><span>Library</span></div>
        <div class="metric"><strong>${esc(me.review_count || 0)}</strong><span>Review</span></div>
        <div class="metric"><strong>${esc(me.songs_edited || 0)}</strong><span>Songs edited</span></div>
        <div class="metric"><strong>${esc(formatSince(me.user_since))}</strong><span>Here since</span></div>
      </div>
      <div class="tiles">
        <button type="button" class="tile" data-go="review"><strong>Review pile</strong><span>Promote drafts to your library.</span></button>
        <button type="button" class="tile" data-go="suggest"><strong>Suggestions</strong><span>Nearby tracks from what you already have.</span></button>
        <button type="button" class="tile" data-go="settings"><strong>Settings</strong><span>Default save is ${esc(destLabel(me.default_dest))}.</span></button>
      </div>`;
  }

  function settingsBody() {
    const driveOn = Boolean(me && me.logged_in);
    const email = (me && me.google_email) || "Drive on";
    const months = me && me.inactive_months != null ? me.inactive_months : 3;
    return `
      <div class="card">
        <div class="row-between">
          <h2>Google Drive</h2>
          <span class="pill${driveOn ? " on" : ""}">${driveOn ? "Connected" : "Off"}</span>
        </div>
        <p class="muted">${driveOn ? esc(email) : "The bot can only write folders it creates and files you sent it. It cannot read the rest of Drive."}</p>
        ${
          driveOn
            ? `<button type="button" class="btn btn-ghost mt" data-act="unlink">Unlink Drive</button>`
            : `<button type="button" class="btn btn-primary mt" data-act="connect">Connect Google Drive</button>`
        }
      </div>
      <div class="card">
        <h2>Default save</h2>
        <p class="muted">Where new saves go when Drive is connected.</p>
        ${destSeg((me && me.default_dest) || "none")}
      </div>
      <p class="muted">Idle sessions are forgotten after ${esc(months)} months. Copies already in Drive stay until you delete them.</p>`;
  }

  function renderSettings() {
    hideMainButton();
    setWaiting(false);
    mainEl.innerHTML = settingsBody();
  }

  function renderLoginIdle() {
    hideMainButton();
    mainEl.innerHTML = `
      <div class="card">
        <h2>Connect Google Drive</h2>
        <p class="muted">Optional. The bot can only write folders it creates and the music you sent it in Telegram. It cannot read the rest of your Drive.</p>
        <button type="button" class="btn btn-primary mt" data-act="connect">Connect Google Drive</button>
      </div>`;
    setMainButton("Connect Google Drive", () => {
      startConnect();
    });
  }

  function renderLoginWaiting() {
    mainEl.innerHTML = `
      <div class="card center">
        <div class="spinner" aria-hidden="true"></div>
        <h2>Finish in the browser</h2>
        <p class="muted">Google is opening outside Telegram. Come back here when Drive is connected.</p>
        <button type="button" class="btn btn-primary mt" data-act="connect">Open Google again</button>
        <button type="button" class="btn btn-ghost" data-act="cancel-login">Cancel</button>
      </div>`;
  }

  function renderLoginTimeout() {
    setWaiting(true);
    hideTabs();
    mainEl.innerHTML = `
      <div class="card center">
        <h2>Still waiting</h2>
        <p class="muted">No Drive login yet. Open Google again, or cancel and stay disconnected.</p>
        <button type="button" class="btn btn-primary mt" data-act="connect">Open Google again</button>
        <button type="button" class="btn btn-ghost" data-act="cancel-login">Cancel</button>
      </div>`;
    setMainButton("Open Google again", () => {
      startConnect();
    });
  }

  function renderLogin() {
    if (me && me.logged_in) {
      page = "settings";
      history.replaceState({ page }, "", "/app/settings");
      heading.textContent = TITLES.settings;
      renderSettings();
      showTabs();
      return;
    }
    if (!autoLoginDone) {
      autoLoginDone = true;
      startConnect();
      return;
    }
    renderLoginIdle();
    showTabs();
  }

  async function loadReview() {
    mainEl.innerHTML = skeletons(3);
    const data = await api("/api/review");
    const tracks = data.tracks || [];
    if (!tracks.length) {
      mainEl.innerHTML = `
        <div class="empty">
          <h2>Review is empty</h2>
          <p class="muted">👎 on a tagged song in Telegram to park it here. Promote it when you want it in the library.</p>
        </div>`;
      return;
    }
    mainEl.innerHTML = tracks
      .map((track) => {
        const bits = trackBits(track);
        const path = track.relative_path || track.file_name || "";
        const drive = track.drive_url
          ? `<p class="muted"><a class="link" href="${esc(track.drive_url)}" data-open="${esc(track.drive_url)}">Open in Drive</a></p>`
          : "";
        return `<article class="card track" data-track="${esc(track.id)}">
          <h2>${esc(bits.artist)} — ${esc(bits.title)}</h2>
          <p class="muted">${bits.album ? esc(bits.album) : "No album"}</p>
          ${path ? `<p class="muted">${esc(path)}</p>` : ""}
          ${drive}
          <div class="actions">
            <button type="button" class="btn btn-primary" data-act="library" data-id="${esc(track.id)}">To library</button>
            <button type="button" class="btn btn-ghost" data-act="draft" data-id="${esc(track.id)}">Keep in review</button>
            <button type="button" class="btn btn-danger" data-act="cancel" data-id="${esc(track.id)}">Remove</button>
          </div>
        </article>`;
      })
      .join("");
  }

  async function loadSuggest(query) {
    suggestQuery = query || "";
    mainEl.innerHTML = `
      <form class="search" id="suggest-form">
        <input type="search" name="q" value="${esc(suggestQuery)}" placeholder="Artist, mood, or leave blank" enterkeyhint="search">
        <button class="btn btn-primary" type="submit">Go</button>
      </form>
      ${skeletons(3)}`;
    const data = await api("/api/suggest?q=" + encodeURIComponent(suggestQuery));
    const results = data.results || [];
    const form = `
      <form class="search" id="suggest-form">
        <input type="search" name="q" value="${esc(suggestQuery)}" placeholder="Artist, mood, or leave blank" enterkeyhint="search">
        <button class="btn btn-primary" type="submit">Go</button>
      </form>`;
    if (!data.lastfm) {
      mainEl.innerHTML =
        form +
        `<div class="empty"><h2>Suggestions are off</h2><p class="muted">Last.fm is not configured on this bot.</p></div>`;
      return;
    }
    if (!(me.library_count || 0) && !results.length) {
      mainEl.innerHTML =
        form +
        `<div class="empty"><h2>Library is empty</h2><p class="muted">Save a few tracks first. Suggest looks at what you already have.</p></div>`;
      return;
    }
    if (!results.length) {
      mainEl.innerHTML =
        form +
        `<div class="empty"><h2>Nothing nearby</h2><p class="muted">Try another word, or leave the box empty.</p></div>`;
      return;
    }
    mainEl.innerHTML =
      form +
      results
        .map((row) => {
          const why = row.why ? `<p class="muted">${esc(row.why)}</p>` : "";
          const owned = row.in_library ? `<span class="pill on">In library</span>` : "";
          const url = row.url || "";
          return `<article class="card hit"${url ? ` data-open="${esc(url)}"` : ""}>
            <div class="row-between">
              <h2>${esc(row.artist)} — ${esc(row.title)}</h2>
              ${owned}
            </div>
            ${why}
          </article>`;
        })
        .join("");
  }

  function gate() {
    hideTabs();
    hideMainButton();
    heading.textContent = "MetaMusic";
    mainEl.innerHTML = `
      <div class="empty">
        <h2>Open this from Telegram</h2>
        <p class="muted">The Mini App has to start from ${esc(BOT)} so we know it is you.</p>
      </div>`;
  }

  async function render(nextPage) {
    page = nextPage;
    heading.textContent = TITLES[page] || "MetaMusic";
    clearBanner();
    hideMainButton();
    if (!tg || !tg.initData) {
      gate();
      return;
    }
    if (page !== "login") {
      stopPoll();
      setWaiting(false);
    }
    try {
      if (!me) {
        mainEl.innerHTML = skeletons(3);
        me = await api("/api/me");
      }
      if (page === "home") {
        renderHome();
        showTabs();
      } else if (page === "settings") {
        renderSettings();
        showTabs();
      } else if (page === "login") {
        renderLogin();
      } else if (page === "review") {
        showTabs();
        await loadReview();
      } else if (page === "suggest") {
        showTabs();
        await loadSuggest(suggestQuery);
      }
    } catch (err) {
      haptic("error");
      showBanner(err);
      mainEl.innerHTML = `<div class="empty"><h2>Could not load</h2><p class="muted">${esc((err && err.error) || "Failed")}</p></div>`;
      if (tg && tg.initData) showTabs();
    }
  }

  function go(next, extraQuery) {
    stopPoll();
    setWaiting(false);
    hideMainButton();
    if (next === "suggest" && extraQuery != null) suggestQuery = extraQuery;
    const path = next === "home" ? "/app" : "/app/" + next;
    const search =
      next === "suggest" && suggestQuery ? "?q=" + encodeURIComponent(suggestQuery) : "";
    history.pushState({ page: next }, "", path + search);
    render(next);
  }

  async function reviewAction(id, action, btn) {
    if (action === "cancel") {
      const ok = await confirmRemove();
      if (!ok) return;
    }
    if (btn) btn.disabled = true;
    try {
      await api("/api/review/" + id + "/action", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action }),
      });
      haptic(action === "cancel" ? "success" : "success");
      const card = mainEl.querySelector(`[data-track="${id}"]`);
      if (card) card.remove();
      if (me && action === "library" && me.review_count) me.review_count = Math.max(0, me.review_count - 1);
      if (me && action === "cancel" && me.review_count) me.review_count = Math.max(0, me.review_count - 1);
      if (me && action === "library") me.library_count = (me.library_count || 0) + 1;
      if (!mainEl.querySelector("[data-track]")) {
        mainEl.innerHTML = `<div class="empty"><h2>Review is empty</h2><p class="muted">Nothing left in this pile.</p></div>`;
      }
    } catch (err) {
      haptic("error");
      showBanner(err);
      if (btn) btn.disabled = false;
    }
  }

  async function setDest(dest) {
    try {
      const data = await api("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ default_dest: dest }),
      });
      me.default_dest = data.default_dest;
      haptic("light");
      mainEl.querySelectorAll("[data-dest]").forEach((btn) => {
        btn.classList.toggle("is-on", btn.dataset.dest === dest);
      });
    } catch (err) {
      haptic("error");
      showBanner(err);
    }
  }

  async function unlinkDrive() {
    const ok = await confirmUnlink();
    if (!ok) return;
    try {
      await api("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ unlink: true }),
      });
      me.logged_in = false;
      me.google_email = null;
      haptic("success");
      renderSettings();
    } catch (err) {
      haptic("error");
      showBanner(err);
    }
  }

  mainEl.addEventListener("click", (event) => {
    const open = event.target.closest("[data-open]");
    if (open && open.dataset.open) {
      event.preventDefault();
      openExternal(open.dataset.open);
      return;
    }
    const goBtn = event.target.closest("[data-go]");
    if (goBtn) {
      go(goBtn.dataset.go);
      return;
    }
    const act = event.target.closest("[data-act]");
    if (!act) return;
    const kind = act.dataset.act;
    if (kind === "connect") startConnect();
    else if (kind === "cancel-login") cancelConnect();
    else if (kind === "unlink") unlinkDrive();
    else if (kind === "library" || kind === "draft" || kind === "cancel") {
      reviewAction(act.dataset.id, kind, act);
    }
  });

  mainEl.addEventListener("click", (event) => {
    const dest = event.target.closest("[data-dest]");
    if (dest) setDest(dest.dataset.dest);
  });

  mainEl.addEventListener("submit", (event) => {
    const form = event.target.closest("#suggest-form");
    if (!form) return;
    event.preventDefault();
    const q = (form.q && form.q.value) || "";
    go("suggest", q);
  });

  tabs.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-page]");
    if (!btn) return;
    go(btn.dataset.page);
  });

  window.addEventListener("popstate", () => {
    stopPoll();
    setWaiting(false);
    hideMainButton();
    suggestQuery = new URLSearchParams(location.search).get("q") || suggestQuery;
    render(pageFromPath());
  });

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && polling) {
      pingMe().catch(() => {});
    }
  });

  applyTheme();
  page = pageFromPath();
  render(page);
})();
