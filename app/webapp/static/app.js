(() => {
  const tg = window.Telegram && window.Telegram.WebApp;
  const BOT = "@MetaMusicProBot";
  const FORMAT_IDS = ["flac", "mp3", "m4a", "ogg", "opus", "wav"];
  const TAG_FIELDS = [
    "title",
    "album",
    "artist",
    "albumartist",
    "composer",
    "genre",
    "date",
    "tracknumber",
    "discnumber",
    "lyrics",
  ];
  const REVIEW_TTL = 10 * 60 * 1000;
  const LINK_ICONS = [
    ["youtube", "YouTube Music", '<path d="M8 5.5v13l11-6.5z"/>'],
    ["apple", "Apple Music", '<path d="M16.2 12.4c0-2.2 1.8-3.2 1.9-3.3-1-1.5-2.6-1.7-3.2-1.7-1.3-.1-2.6.8-3.3.8s-1.7-.8-2.9-.7c-1.5.1-2.9.9-3.6 2.2-1.6 2.7-.4 6.7 1.1 8.9.8 1.1 1.7 2.3 2.9 2.2 1.1-.1 1.6-.8 3-.8s1.8.8 3 .7c1.2-.1 2-1.1 2.8-2.2.9-1.3 1.2-2.5 1.3-2.6-.1 0-2.4-1-2.4-3.5zM14.3 6.5c.6-.8 1.1-1.8.9-2.9-1 .1-2.1.7-2.7 1.5-.6.7-1.1 1.8-.9 2.8 1.1.1 2.1-.6 2.7-1.4z"/>'],
    ["lastfm", "Last.fm", '<path d="M10.4 16.4c-1.7 0-3.2-1-3.2-3.1 0-2.5 1.8-3.6 3.7-3.6.7 0 1.4.1 1.6.2l-.3 1.7c-.3-.1-.7-.2-1.2-.2-1.1 0-2 .6-2 1.8 0 1.1.7 1.6 1.6 1.6.9 0 1.5-.4 2.1-1.5l1.6-3.3c.6-1.3 1.4-2.2 3.3-2.2 1.9 0 3.1 1.2 3.1 3.3 0 2.3-1.5 4.6-4.1 4.6-1.1 0-2-.4-2.6-1.1l-.4 3.4H12l.9-5.4c-.6 1.1-1.6 1.8-2.5 1.8zm7.1-1.6c1.3 0 2.1-1.4 2.1-2.9 0-1.2-.5-1.9-1.5-1.9-.8 0-1.4.5-1.8 1.4l-.7 1.7c.4.9 1.1 1.7 1.9 1.7z"/>'],
    ["musicbrainz", "MusicBrainz", '<path d="M7 4h3.2l4.3 8.6L18.8 4H22v16h-3.1V9.7L15.2 16h-2.4L9.1 9.8V20H6V4h1z"/>'],
    ["google", "Google", '<path d="M21 12.2c0-.7-.1-1.4-.2-2H12v3.8h5.1c-.2 1.2-.9 2.2-1.9 2.9v2.4h3.1c1.8-1.7 2.7-4.1 2.7-7.1z"/><path d="M12 21c2.5 0 4.7-.8 6.2-2.2l-3.1-2.4c-.8.6-1.9.9-3.1.9-2.4 0-4.4-1.6-5.1-3.8H3.7v2.5C5.2 18.9 8.3 21 12 21z"/><path d="M6.9 13.5c-.2-.6-.3-1.2-.3-1.5s.1-1 .3-1.5V8H3.7C3.3 8.8 3 9.9 3 12s.3 3.2.7 4l3.2-2.5z"/><path d="M12 6.5c1.4 0 2.6.5 3.6 1.4l2.7-2.7C16.7 3.7 14.5 3 12 3 8.3 3 5.2 5.1 3.7 8l3.2 2.5C7.6 8.1 9.6 6.5 12 6.5z"/>'],
    ["hifi", "HiFiAudioBot", '<path d="M3 11.5 21 4l-7.8 16.2-2.3-5.3z"/>'],
  ];

  const mainEl = document.getElementById("main");
  const heading = document.getElementById("heading");
  const banner = document.getElementById("banner");
  const tabs = document.getElementById("tabs");
  const refreshBtn = document.getElementById("refresh-btn");
  const TITLES = {
    home: "Home",
    review: "Review",
    suggest: "Suggest",
    settings: "Settings",
    login: "Connect Drive",
  };

  let me = null;
  let page = "home";
  let renderGen = 0;
  let pageAbort = null;
  let artAbort = null;
  let pollTimer = null;
  let reviewTimer = null;
  let simTimer = null;
  let polling = false;
  let autoLoginDone = false;
  let mainHandler = null;
  let suggestQuery = new URLSearchParams(location.search).get("q") || "";
  let reviewCache = { tracks: null, fetchedAt: 0 };
  let suggestCache = { q: null, data: null };
  let detailRow = null;

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
    if (tg && String(url).startsWith("https://t.me/") && tg.openTelegramLink) {
      tg.openTelegramLink(url);
      return;
    }
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

  function takeover() {
    if (pageAbort) pageAbort.abort();
    pageAbort = new AbortController();
    return pageAbort.signal;
  }

  function stillOn(gen, want) {
    return gen === renderGen && page === want;
  }

  function setRefreshing(on) {
    if (refreshBtn) refreshBtn.classList.toggle("is-spin", Boolean(on));
  }

  function setRefreshVisible(on) {
    if (!refreshBtn) return;
    refreshBtn.hidden = !on;
    if (!on) refreshBtn.classList.remove("is-spin");
  }

  async function api(path, opts) {
    const headers = Object.assign(
      { "X-Telegram-Init-Data": tg ? tg.initData : "" },
      (opts && opts.headers) || {},
    );
    let res;
    try {
      res = await fetch(path, Object.assign({}, opts, { headers }));
    } catch (err) {
      if (err && (err.name === "AbortError" || err.code === 20)) throw { aborted: true };
      throw { ok: false, error: "Failed" };
    }
    let data;
    try {
      data = await res.json();
    } catch (err) {
      if (err && err.name === "AbortError") throw { aborted: true };
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

  function stopReviewTimer() {
    if (reviewTimer) {
      clearTimeout(reviewTimer);
      reviewTimer = null;
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

  function confirmText(text) {
    if (tg && tg.showConfirm) {
      return new Promise((resolve) => tg.showConfirm(text, resolve));
    }
    return Promise.resolve(window.confirm(text));
  }

  function confirmUnlink() {
    return confirmText("Unlink Google Drive? The bot will forget this login.");
  }

  function formatLabel(list) {
    const formats = Array.isArray(list) && list.length ? list : ["flac", "mp3"];
    return formats.map((fmt) => String(fmt).toUpperCase()).join(", ");
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

  function formatSeg(selected) {
    const on = new Set(Array.isArray(selected) && selected.length ? selected : ["flac", "mp3"]);
    return `<div class="seg wrap" role="group" aria-label="Listen for">
      ${FORMAT_IDS.map(
        (fmt) =>
          `<button type="button" class="seg-btn${on.has(fmt) ? " is-on" : ""}" data-format="${fmt}">${fmt.toUpperCase()}</button>`,
      ).join("")}
    </div>`;
  }

  function similarityValue() {
    const raw = me && me.suggest_similarity;
    const n = raw == null ? 0.5 : Number(raw);
    if (Number.isNaN(n)) return 0.5;
    return Math.min(1, Math.max(0, n));
  }

  function patchHomeMetrics() {
    if (page !== "home" || !me) return;
    const map = [
      ["library_count", me.library_count || 0],
      ["review_count", me.review_count || 0],
      ["songs_edited", me.songs_edited || 0],
    ];
    map.forEach(([key, val]) => {
      const el = mainEl.querySelector(`[data-metric="${key}"]`);
      if (el) el.textContent = String(val);
    });
  }

  async function refreshMeSoft(gen, signal) {
    try {
      const next = await api("/api/me", { signal });
      if (!stillOn(gen, "home")) return;
      me = next;
      patchHomeMetrics();
    } catch (err) {
      if (err && err.aborted) return;
    }
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
        <div class="metric"><strong data-metric="library_count">${esc(me.library_count || 0)}</strong><span>Library</span></div>
        <div class="metric"><strong data-metric="review_count">${esc(me.review_count || 0)}</strong><span>Review</span></div>
        <div class="metric"><strong data-metric="songs_edited">${esc(me.songs_edited || 0)}</strong><span>Songs edited</span></div>
        <div class="metric"><strong>${esc(formatSince(me.user_since))}</strong><span>Here since</span></div>
      </div>
      <div class="tiles">
        <button type="button" class="tile" data-go="review"><strong>Review pile</strong><span>Edit tags, then move into your library.</span></button>
        <button type="button" class="tile" data-go="suggest"><strong>Suggestions</strong><span>Nearby tracks from what you already have.</span></button>
        <button type="button" class="tile" data-go="settings"><strong>Settings</strong><span>Listen for ${esc(formatLabel(me.allowed_formats))}. Default save is ${esc(destLabel(me.default_dest))}.</span></button>
      </div>`;
  }

  function settingsBody() {
    const driveOn = Boolean(me && me.logged_in);
    const email = (me && me.google_email) || "Drive on";
    const months = me && me.inactive_months != null ? me.inactive_months : 3;
    const sim = Math.round(similarityValue() * 100);
    const allow = Boolean(me && me.suggest_allow_dissimilar);
    const correct = Boolean(me && me.correct_telegram);
    const skipAsk = Boolean(me && me.skip_save_prompt);
    const deleteOrig = Boolean(me && me.delete_original);
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
        <h2>Suggestions</h2>
        <p class="muted">How close results should stay to songs you already know.</p>
        <input class="slider" id="sim-slider" type="range" min="0" max="100" value="${sim}" aria-label="Familiarity">
        <div class="slider-row"><span>Explorer</span><span>Familiar</span></div>
        <button type="button" class="check-btn${allow ? " is-on" : ""}" data-act="toggle-dissimilar">${
          allow ? "Unlike-library songs on" : "Allow songs unlike your library"
        }</button>
      </div>
      <div class="card">
        <h2>Default save</h2>
        <p class="muted">Where new saves go when Drive is connected. Do not ask again skips the Telegram prompt and uses these toggles. Delete original file removes the message you sent after a successful save.</p>
        ${destSeg((me && me.default_dest) || "none")}
        <button type="button" class="check-btn${correct ? " is-on" : ""}" data-act="toggle-correct-tg">${
          correct ? "Correct Telegram file on" : "Correct Telegram file"
        }</button>
        <button type="button" class="check-btn${deleteOrig ? " is-on" : ""}" data-act="toggle-delete-orig">${
          deleteOrig ? "Delete original file on" : "Delete original file"
        }</button>
        <button type="button" class="check-btn${skipAsk ? " is-on" : ""}" data-act="toggle-skip-save">${
          skipAsk ? "Do not ask again on" : "Do not ask again"
        }</button>
      </div>
      <div class="card">
        <h2>Listen for</h2>
        <p class="muted">Which files the bot tags for you. FLAC and MP3 start on; the rest stay off until you tap them.</p>
        ${formatSeg(me && me.allowed_formats)}
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

  function field(name, label, value, extra) {
    return `<label class="field">${esc(label)}<input data-field="${name}" value="${esc(value || "")}" ${extra || ""}></label>`;
  }

  function reviewCard(track) {
    const path = track.relative_path || track.file_name || "";
    const drive = track.drive_url
      ? `<p class="muted"><a class="link" href="${esc(track.drive_url)}" data-open="${esc(track.drive_url)}">Open in Drive</a></p>`
      : "";
    return `<article class="card track" data-track="${esc(track.id)}">
      <div class="fields">
        ${field("artist", "Artist", track.artist)}
        ${field("title", "Title", track.title)}
        ${field("album", "Album", track.album)}
        ${field("albumartist", "Album artist", track.albumartist)}
        ${field("composer", "Composer", track.composer)}
        ${field("genre", "Genre", track.genre, 'placeholder="genre | mood | language | instrument"')}
        <div class="field-row">
          ${field("date", "Date", track.date)}
          ${field("tracknumber", "Track", track.tracknumber)}
          ${field("discnumber", "Disc", track.discnumber)}
        </div>
        <label class="field">Lyrics<textarea data-field="lyrics">${esc(track.lyrics || "")}</textarea></label>
      </div>
      ${path ? `<p class="muted">${esc(path)}</p>` : ""}
      ${drive}
      <div class="actions">
        <button type="button" class="btn btn-primary" data-act="save-tags" data-id="${esc(track.id)}">Save tags</button>
        <button type="button" class="btn btn-ghost" data-act="library" data-id="${esc(track.id)}">To library</button>
        <button type="button" class="btn btn-danger" data-act="cancel" data-id="${esc(track.id)}">Delete</button>
      </div>
    </article>`;
  }

  function paintReview(tracks) {
    if (tracks == null) {
      mainEl.innerHTML = skeletons(3);
      return;
    }
    if (!tracks.length) {
      mainEl.innerHTML = `
        <div class="empty">
          <h2>Review is empty</h2>
          <p class="muted">👎 on a tagged song in Telegram to park it here. Edit tags, then move it to the library.</p>
        </div>`;
      return;
    }
    mainEl.innerHTML = tracks.map(reviewCard).join("");
  }

  function canReplaceReview() {
    const el = document.activeElement;
    if (!el || !mainEl.contains(el)) return true;
    const tag = el.tagName;
    return tag !== "INPUT" && tag !== "TEXTAREA";
  }

  function scheduleReviewTimer() {
    stopReviewTimer();
    if (page !== "review" || !reviewCache.fetchedAt) return;
    const wait = Math.max(1000, REVIEW_TTL - (Date.now() - reviewCache.fetchedAt));
    reviewTimer = setTimeout(() => {
      if (page === "review") loadReview({ gen: renderGen, force: false });
    }, wait);
  }

  async function loadReview({ gen, force }) {
    const still = () => stillOn(gen, "review");
    const hasCache = Array.isArray(reviewCache.tracks);
    const stale = !reviewCache.fetchedAt || Date.now() - reviewCache.fetchedAt >= REVIEW_TTL;
    if (hasCache && !force) {
      paintReview(reviewCache.tracks);
      if (!stale) {
        scheduleReviewTimer();
        return;
      }
    } else if (!hasCache) {
      paintReview(null);
    }
    setRefreshing(true);
    try {
      const data = await api("/api/review", { signal: pageAbort && pageAbort.signal });
      if (!still()) return;
      reviewCache = { tracks: data.tracks || [], fetchedAt: Date.now() };
      if (force || canReplaceReview()) paintReview(reviewCache.tracks);
    } catch (err) {
      if (err && err.aborted) return;
      if (!still()) return;
      if (!hasCache) throw err;
      showBanner(err);
    } finally {
      if (still()) {
        setRefreshing(false);
        scheduleReviewTimer();
      }
    }
  }

  function readCardTags(card) {
    const out = {};
    TAG_FIELDS.forEach((name) => {
      const input = card.querySelector(`[data-field="${name}"]`);
      if (input) out[name] = input.value;
    });
    return out;
  }

  function updateReviewCache(id, next) {
    if (!Array.isArray(reviewCache.tracks)) return;
    if (next == null) {
      reviewCache.tracks = reviewCache.tracks.filter((track) => String(track.id) !== String(id));
      return;
    }
    reviewCache.tracks = reviewCache.tracks.map((track) =>
      String(track.id) === String(id) ? Object.assign({}, track, next) : track,
    );
  }

  function suggestFormHtml() {
    return `<form class="search" id="suggest-form">
      <input type="search" name="q" value="${esc(suggestQuery)}" placeholder="Artist, mood, or leave blank" enterkeyhint="search">
      <button class="btn btn-primary" type="submit">Go</button>
    </form><div id="suggest-results"></div>`;
  }

  function ensureSuggestShell() {
    if (mainEl.querySelector("#suggest-form") && mainEl.querySelector("#suggest-results")) return;
    mainEl.innerHTML = suggestFormHtml();
  }

  function linkIcons(links) {
    return `<div class="link-row">${LINK_ICONS.map(([key, label, path]) => {
      const url = links && links[key];
      const on = Boolean(url);
      return `<button type="button" class="link-ico${on ? "" : " is-off"}"${
        on ? ` data-open="${esc(url)}"` : " disabled"
      } aria-label="${esc(label)}" title="${esc(label)}"><svg viewBox="0 0 24 24" aria-hidden="true">${path}</svg></button>`;
    }).join("")}</div>`;
  }

  function paintSuggestResults(data) {
    const box = document.getElementById("suggest-results");
    if (!box) return;
    const results = (data && data.results) || [];
    if (!data || !data.lastfm) {
      box.innerHTML = `<div class="empty"><h2>Suggestions are off</h2><p class="muted">Last.fm is not configured on this bot.</p></div>`;
      return;
    }
    if (!(me && me.library_count) && !results.length) {
      box.innerHTML = `<div class="empty"><h2>Library is empty</h2><p class="muted">Save a few tracks first. Suggest looks at what you already have.</p></div>`;
      return;
    }
    if (!results.length) {
      box.innerHTML = `<div class="empty"><h2>Nothing nearby</h2><p class="muted">Try another word, or leave the box empty.</p></div>`;
      return;
    }
    box.innerHTML = results
      .map((row, index) => {
        const why = row.why ? `<p class="muted">${esc(row.why)}</p>` : "";
        const owned = row.in_library ? `<span class="pill on">In library</span>` : "";
        return `<article class="card hit" data-hit="${index}">
          <div class="row-between">
            <h2>${esc(row.artist)} — ${esc(row.title)}</h2>
            ${owned}
          </div>
          ${why}
        </article>`;
      })
      .join("");
  }

  async function loadSuggest(query, { gen, force }) {
    const still = () => stillOn(gen, "suggest");
    suggestQuery = query || "";
    ensureSuggestShell();
    const input = mainEl.querySelector("#suggest-form [name=q]");
    if (input && document.activeElement !== input) input.value = suggestQuery;
    const cached = suggestCache.data && suggestCache.q === suggestQuery;
    if (cached && !force) {
      paintSuggestResults(suggestCache.data);
      return;
    }
    const box = document.getElementById("suggest-results");
    if (box) box.innerHTML = skeletons(3);
    const data = await api("/api/suggest?q=" + encodeURIComponent(suggestQuery), {
      signal: pageAbort && pageAbort.signal,
    });
    if (!still()) return;
    suggestCache = { q: suggestQuery, data };
    paintSuggestResults(data);
  }

  function cancelArt() {
    if (artAbort) {
      artAbort.abort();
      artAbort = null;
    }
  }

  function paintCarousel(urls) {
    const el = document.getElementById("art-carousel");
    if (!el) return;
    if (!urls || !urls.length) {
      el.innerHTML = `<div class="art-ph" aria-hidden="true"></div>`;
      return;
    }
    el.innerHTML = urls
      .map((url) => `<img src="${esc(url)}" alt="" loading="lazy" decoding="async">`)
      .join("");
  }

  function openSuggestDetail(index) {
    const rows = (suggestCache.data && suggestCache.data.results) || [];
    const row = rows[index];
    if (!row) return;
    detailRow = row;
    cancelArt();
    const why = row.why ? `<p class="muted">${esc(row.why)}</p>` : "";
    const owned = row.in_library ? `<span class="pill on">In library</span>` : "";
    mainEl.innerHTML = `
      <button type="button" class="btn btn-ghost back-btn" data-act="suggest-back">Back</button>
      <article class="card">
        <div class="carousel" id="art-carousel"><div class="art-ph" aria-hidden="true"></div></div>
        <div class="row-between">
          <h2>${esc(row.artist)} — ${esc(row.title)}</h2>
          ${owned}
        </div>
        ${why}
        ${linkIcons(row.links || {})}
      </article>`;
    loadArt(row);
  }

  function closeSuggestDetail() {
    cancelArt();
    detailRow = null;
    mainEl.innerHTML = suggestFormHtml();
    if (suggestCache.data) paintSuggestResults(suggestCache.data);
  }

  async function loadArt(row) {
    cancelArt();
    artAbort = new AbortController();
    const signal = artAbort.signal;
    const params = new URLSearchParams({
      artist: row.artist || "",
      title: row.title || "",
    });
    if (row.mbid) params.set("mbid", row.mbid);
    try {
      const data = await api("/api/suggest/art?" + params.toString(), { signal });
      if (signal.aborted || page !== "suggest") return;
      paintCarousel(data.covers || []);
      if (data.apple) {
        row.links = Object.assign({}, row.links || {}, { apple: data.apple });
        const appleBtn = mainEl.querySelector('.link-ico[aria-label="Apple Music"]');
        if (appleBtn) {
          appleBtn.classList.remove("is-off");
          appleBtn.disabled = false;
          appleBtn.dataset.open = data.apple;
        }
      }
    } catch (err) {
      if (err && err.aborted) return;
      paintCarousel([]);
    }
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
    const gen = ++renderGen;
    const signal = takeover();
    cancelArt();
    detailRow = null;
    page = nextPage;
    heading.textContent = TITLES[page] || "MetaMusic";
    clearBanner();
    hideMainButton();
    setRefreshVisible(page === "review");
    if (page !== "review") stopReviewTimer();
    if (!tg || !tg.initData) {
      gate();
      return;
    }
    if (page !== "login") {
      stopPoll();
      setWaiting(false);
    }
    const hadMe = Boolean(me);
    try {
      if (!me) {
        mainEl.innerHTML = skeletons(3);
        me = await api("/api/me", { signal });
        if (!stillOn(gen, nextPage)) return;
      }
      if (page === "home") {
        renderHome();
        showTabs();
        if (hadMe) refreshMeSoft(gen, signal);
      } else if (page === "settings") {
        renderSettings();
        showTabs();
      } else if (page === "login") {
        renderLogin();
      } else if (page === "review") {
        showTabs();
        await loadReview({ gen, force: false });
      } else if (page === "suggest") {
        showTabs();
        await loadSuggest(suggestQuery, { gen, force: false });
      }
    } catch (err) {
      if (err && err.aborted) return;
      if (!stillOn(gen, nextPage)) return;
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
    const card = mainEl.querySelector(`[data-track="${id}"]`);
    if (action === "cancel") {
      const ok = await confirmText("Delete this track from review?");
      if (!ok) return;
    }
    if (action === "library") {
      const text = me && me.logged_in
        ? "Move this track to your library?"
        : "Not logged in to Google Drive. Move locally anyway? Drive copy is skipped until you connect Drive.";
      const ok = await confirmText(text);
      if (!ok) return;
    }
    if (btn) btn.disabled = true;
    const tags = card ? readCardTags(card) : {};
    try {
      const data = await api("/api/review/" + id + "/action", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.assign({ action }, tags)),
      });
      haptic("success");
      if (action === "tags") {
        if (data.track) updateReviewCache(id, data.track);
        if (btn) btn.disabled = false;
        return;
      }
      if (card) card.remove();
      updateReviewCache(id, null);
      if (me && (action === "library" || action === "cancel") && me.review_count) {
        me.review_count = Math.max(0, me.review_count - 1);
      }
      if (me && action === "library") me.library_count = (me.library_count || 0) + 1;
      if (!mainEl.querySelector("[data-track]")) {
        paintReview([]);
      }
    } catch (err) {
      haptic("error");
      showBanner(err);
      if (btn) btn.disabled = false;
    }
  }

  async function saveSavePrefs(extra) {
    const body = Object.assign(
      {
        default_dest: (me && me.default_dest) || "none",
        correct_telegram: Boolean(me && me.correct_telegram),
        skip_save_prompt: Boolean(me && me.skip_save_prompt),
        delete_original: Boolean(me && me.delete_original),
      },
      extra || {},
    );
    try {
      const data = await api("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      me.default_dest = data.default_dest;
      me.correct_telegram = data.correct_telegram;
      me.skip_save_prompt = data.skip_save_prompt;
      me.delete_original = data.delete_original;
      return data;
    } catch (err) {
      haptic("error");
      showBanner(err);
      return null;
    }
  }

  async function setDest(dest) {
    const data = await saveSavePrefs({ default_dest: dest });
    if (!data) return;
    haptic("light");
    mainEl.querySelectorAll("[data-dest]").forEach((btn) => {
      btn.classList.toggle("is-on", btn.dataset.dest === dest);
    });
  }

  async function toggleCorrectTelegram() {
    if (!me) return;
    me.correct_telegram = !me.correct_telegram;
    const btn = mainEl.querySelector("[data-act=toggle-correct-tg]");
    if (btn) {
      btn.classList.toggle("is-on", me.correct_telegram);
      btn.textContent = me.correct_telegram ? "Correct Telegram file on" : "Correct Telegram file";
    }
    haptic("light");
    await saveSavePrefs({ correct_telegram: me.correct_telegram });
  }

  async function toggleSkipSave() {
    if (!me) return;
    me.skip_save_prompt = !me.skip_save_prompt;
    const btn = mainEl.querySelector("[data-act=toggle-skip-save]");
    if (btn) {
      btn.classList.toggle("is-on", me.skip_save_prompt);
      btn.textContent = me.skip_save_prompt ? "Do not ask again on" : "Do not ask again";
    }
    haptic("light");
    await saveSavePrefs({ skip_save_prompt: me.skip_save_prompt });
  }

  async function toggleDeleteOriginal() {
    if (!me) return;
    me.delete_original = !me.delete_original;
    const btn = mainEl.querySelector("[data-act=toggle-delete-orig]");
    if (btn) {
      btn.classList.toggle("is-on", me.delete_original);
      btn.textContent = me.delete_original ? "Delete original file on" : "Delete original file";
    }
    haptic("light");
    await saveSavePrefs({ delete_original: me.delete_original });
  }

  async function toggleFormat(fmt) {
    const current = new Set((me && me.allowed_formats) || ["flac", "mp3"]);
    if (current.has(fmt)) current.delete(fmt);
    else current.add(fmt);
    const next = FORMAT_IDS.filter((id) => current.has(id));
    try {
      const data = await api("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ allowed_formats: next }),
      });
      me.allowed_formats = data.allowed_formats;
      haptic("light");
      const on = new Set(me.allowed_formats || []);
      mainEl.querySelectorAll("[data-format]").forEach((btn) => {
        btn.classList.toggle("is-on", on.has(btn.dataset.format));
      });
    } catch (err) {
      haptic("error");
      showBanner(err);
    }
  }

  async function saveSuggestPrefs(extra) {
    const body = Object.assign(
      {
        suggest_similarity: similarityValue(),
        suggest_allow_dissimilar: Boolean(me && me.suggest_allow_dissimilar),
      },
      extra || {},
    );
    try {
      const data = await api("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      me.suggest_similarity = data.suggest_similarity;
      me.suggest_allow_dissimilar = data.suggest_allow_dissimilar;
    } catch (err) {
      haptic("error");
      showBanner(err);
    }
  }

  function onSimInput(value) {
    if (!me) return;
    me.suggest_similarity = Number(value) / 100;
    clearTimeout(simTimer);
    simTimer = setTimeout(() => saveSuggestPrefs(), 280);
  }

  async function toggleDissimilar() {
    if (!me) return;
    me.suggest_allow_dissimilar = !me.suggest_allow_dissimilar;
    const btn = mainEl.querySelector("[data-act=toggle-dissimilar]");
    if (btn) {
      btn.classList.toggle("is-on", me.suggest_allow_dissimilar);
      btn.textContent = me.suggest_allow_dissimilar
        ? "Unlike-library songs on"
        : "Allow songs unlike your library";
    }
    haptic("light");
    await saveSuggestPrefs({ suggest_allow_dissimilar: me.suggest_allow_dissimilar });
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
    const hit = event.target.closest("[data-hit]");
    if (hit && !event.target.closest("[data-act]")) {
      openSuggestDetail(Number(hit.dataset.hit));
      return;
    }
    const act = event.target.closest("[data-act]");
    if (!act) return;
    const kind = act.dataset.act;
    if (kind === "connect") startConnect();
    else if (kind === "cancel-login") cancelConnect();
    else if (kind === "unlink") unlinkDrive();
    else if (kind === "suggest-back") closeSuggestDetail();
    else if (kind === "toggle-dissimilar") toggleDissimilar();
    else if (kind === "toggle-correct-tg") toggleCorrectTelegram();
    else if (kind === "toggle-delete-orig") toggleDeleteOriginal();
    else if (kind === "toggle-skip-save") toggleSkipSave();
    else if (kind === "save-tags") reviewAction(act.dataset.id, "tags", act);
    else if (kind === "library" || kind === "cancel") reviewAction(act.dataset.id, kind, act);
  });

  mainEl.addEventListener("click", (event) => {
    const dest = event.target.closest("[data-dest]");
    if (dest) setDest(dest.dataset.dest);
    const fmt = event.target.closest("[data-format]");
    if (fmt) toggleFormat(fmt.dataset.format);
  });

  mainEl.addEventListener("input", (event) => {
    if (event.target && event.target.id === "sim-slider") onSimInput(event.target.value);
  });

  mainEl.addEventListener("submit", (event) => {
    const form = event.target.closest("#suggest-form");
    if (!form) return;
    event.preventDefault();
    const q = (form.q && form.q.value) || "";
    suggestCache = { q: null, data: null };
    go("suggest", q);
  });

  tabs.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-page]");
    if (!btn) return;
    go(btn.dataset.page);
  });

  if (refreshBtn) {
    refreshBtn.addEventListener("click", () => {
      if (page !== "review") return;
      loadReview({ gen: renderGen, force: true });
    });
  }

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

  document.addEventListener("pointerdown", (event) => {
    const active = document.activeElement;
    if (!active) return;
    const tag = active.tagName;
    if (tag !== "INPUT" && tag !== "TEXTAREA") return;
    const target = event.target;
    if (target === active || active.contains(target)) return;
    if (target && target.closest && target.closest("input, textarea")) return;
    active.blur();
  });

  applyTheme();
  page = pageFromPath();
  render(page);
})();
