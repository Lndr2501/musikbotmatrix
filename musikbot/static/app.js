"use strict";
/* Musikbot-Dashboard: holt den Zustand jede Sekunde und schickt Befehle an /api. */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const ICONS = {
  play: '<svg viewBox="0 0 24 24" class="solid"><path d="M7 4v16l13-8L7 4Z"/></svg>',
  next: '<svg viewBox="0 0 24 24"><path d="M3 6h12M3 12h12M3 18h8M17 14l4 4-4 4"/></svg>',
  plus: '<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>',
  top: '<svg viewBox="0 0 24 24"><path d="M5 4h14M12 20V9M7 13l5-5 5 5"/></svg>',
  remove: '<svg viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg>',
  heart: '<svg viewBox="0 0 24 24"><path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1-1.1a5.5 5.5 0 0 0-7.8 7.8l1 1.1L12 21l7.8-7.5 1-1.1a5.5 5.5 0 0 0 0-7.8Z"/></svg>',
  shuffle: '<svg viewBox="0 0 24 24"><path d="M16 3h5v5M4 20 21 3M21 16v5h-5M15 15l6 6M4 4l5 5"/></svg>',
  list: '<svg viewBox="0 0 24 24"><path d="M3 6h12M3 12h12M3 18h7M18 13v8M14 17h8"/></svg>',
  trash: '<svg viewBox="0 0 24 24"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"/></svg>',
};

let S = null;            // letzter Zustand vom Server
let fetchedAt = 0;       // wann S kam (fuer die laufende Zeit)
let seeking = false;     // Nutzer zieht gerade den Fortschrittsbalken
let sliding = new Set(); // Regler, die gerade bewegt werden (nicht ueberschreiben)
let pollTimer = null;
let LIB = null;          // persoenliche Bibliothek (fuer "Zu Playlist" und den Tab "Meins")
const shown = {};        // zuletzt gezeichneter Stand je Bereich -> nur bei Aenderung neu zeichnen
const changed = (part, value) => {
  const key = JSON.stringify(value);
  if (shown[part] === key) return false;
  shown[part] = key;
  return true;
};
// ---------------------------------------------------------------- API
async function api(path, body) {
  const opts = { credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.headers["X-Musikbot"] = "1";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = {};
  try { data = await res.json(); } catch { /* leer */ }
  if (res.status === 401 && path !== "/api/link") { showLogin(); throw new Error("login"); }
  if (!res.ok) throw new Error(data.error || `Fehler ${res.status}`);
  return data;
}

async function act(path, body, okText) {
  try {
    const data = await api(path, body);
    if (data && data.state !== undefined && data.queue) apply(data);
    if (okText) toast(okText);
    return data;
  } catch (e) {
    if (e.message !== "login") toast(e.message, true);
    return null;
  }
}
const cmd = (name, arg = "", okText) => act("/api/cmd", { name, arg: String(arg) }, okText);

function toast(text, err = false) {
  const el = document.createElement("div");
  el.className = "toast" + (err ? " err" : "");
  el.textContent = text;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), err ? 6000 : 2600);
}

// ---------------------------------------------------------------- Login
function showLogin(error) {
  clearTimeout(pollTimer);
  $("#app").hidden = true;
  $("#login").hidden = false;
  const err = $("#login-error");
  err.hidden = !error;
  err.textContent = error || "";
}

// Login-Link aus der DM: https://…/#login=SCHLUESSEL (der #-Teil geht nie an den Server-Log)
async function linkLogin() {
  const m = location.hash.match(/login=([\w-]+)/);
  if (!m) return;
  history.replaceState(null, "", location.pathname);   // Schluessel aus der Adresszeile entfernen
  try {
    await api("/api/link", { token: m[1] });
  } catch (e) {
    showLogin(e.message);
    throw e;
  }
}

$("#logout").addEventListener("click", async () => {
  await api("/api/logout", {}).catch(() => {});
  showLogin();
});

// ---------------------------------------------------------------- Helfer
function fmt(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return (h ? `${h}:${String(m).padStart(2, "0")}` : m) + ":" + String(s).padStart(2, "0");
}
function setRange(el, value) {
  if (sliding.has(el.id)) return;
  el.value = value;
  paintRange(el);
}
function paintRange(el) {
  const p = ((el.value - el.min) / (el.max - el.min)) * 100;
  el.style.setProperty("--p", p + "%");
}
function button(icon, title, onClick, extra = "") {
  const b = document.createElement("button");
  b.className = "icon-btn " + extra;
  b.innerHTML = ICONS[icon];
  b.title = title;
  b.setAttribute("aria-label", title);
  b.addEventListener("click", (ev) => { ev.stopPropagation(); onClick(ev); });
  return b;
}
function row(item, sub, actions = []) {
  const li = $("#row-tpl").content.firstElementChild.cloneNode(true);
  const img = $("img.thumb", li), empty = $(".thumb-empty", li);
  if (item.thumb || item.thumbnail) { img.src = item.thumb || item.thumbnail; empty.remove(); }
  else img.remove();
  $(".row-title", li).textContent = item.title || item.key || "?";
  if (item.mine) {
    const b = document.createElement("span");
    b.className = "mine-badge"; b.textContent = "Du";
    $(".row-title", li).append(b);
  }
  $(".row-sub", li).textContent = sub;
  const box = $(".row-actions", li);
  actions.forEach((a) => box.append(a));
  return li;
}
function subline(t, extra = "") {
  return [t.artist, t.duration ? fmt(t.duration) : "", extra].filter(Boolean).join(" · ");
}

// ---------------------------------------------------------------- Anzeige
function apply(state) {
  S = state;
  fetchedAt = performance.now();
  render();
}

function render() {
  if (!S) return;
  const t = S.track;
  const player = $("#player");
  player.classList.toggle("paused", S.state !== "playing");
  $("#title").textContent = t ? t.title : "Nichts läuft";
  $("#artist").textContent = t
    ? [t.artist, t.requester === "autoplay" ? "✨ Autoplay" : ""].filter(Boolean).join(" · ") || (t.requester ? `gewünscht von ${t.requester}` : "")
    : "Such dir etwas aus – oder starte das 24/7-Radio.";
  document.title = t ? `${S.state === "playing" ? "▶" : "⏸"} ${t.title} – Musikbot` : "Musikbot";

  const cover = $("#cover"), empty = $("#cover-empty");
  const thumb = t && t.thumb;
  if (thumb) {
    if (cover.getAttribute("src") !== thumb) cover.src = thumb;
    cover.hidden = false; empty.hidden = true;
    $("#backdrop").style.backgroundImage = `url(${JSON.stringify(thumb)})`;
  } else {
    cover.hidden = true; empty.hidden = false;
    $("#backdrop").style.backgroundImage = "";
  }

  setPlayIcon(S.state === "playing");
  const initials = (S.me_name || "?").replace(/^@/, "").slice(0, 1).toUpperCase();
  $("#me-avatar").textContent = initials;
  $("#me-name").textContent = S.me_name || "";
  $("#me").title = S.me;
  $("#loop").classList.toggle("on", S.loop);
  $("#video").classList.toggle("on", S.video);
  $("#autoplay").classList.toggle("on", S.autoplay);
  $("#autoplay-hint").hidden = !S.autoplay || S.queue.some((t) => t.requester === "autoplay");
  $("#fav").classList.toggle("on", !!(t && S.favs.includes(t.key)));
  setRange($("#volume"), S.volume);
  $("#volume-val").textContent = S.volume;

  // Status oben rechts
  if (changed("chips", [S.busy, S.always, S.in_call, S.peers])) renderChips();
  if (changed("queue", [S.queue, S.history, S.me])) renderQueue();
  if (changed("fades", [S.crossfade, S.fade_in])) renderFades();
  tick();
}

function setPlayIcon(playing) {
  // SVG-Elemente kennen die Eigenschaft .hidden nicht – das Attribut direkt setzen
  $("#ic-play").toggleAttribute("hidden", playing);
  $("#ic-pause").toggleAttribute("hidden", !playing);
}

function renderChips() {
  const chips = $("#status-chips");
  chips.replaceChildren();
  const pill = (text, cls = "", hideSm = false) => {
    const el = document.createElement("span");
    el.className = `pill ${cls} ${hideSm ? "hide-sm" : ""}`;
    el.innerHTML = '<span class="dot"></span>';
    el.append(text);
    chips.append(el);
  };
  if (S.busy) pill("lädt …", "busy");
  if (S.always) pill(S.always.kind === "radio" ? "24/7 Radio" : "24/7", "live", true);
  pill(S.in_call ? `im Call · ${S.peers}` : "nicht im Call", S.in_call ? "live" : "");
}

function tick() {
  if (!S) return;
  const t = S.track;
  const dur = t ? t.duration : 0;
  let pos = S.position;
  if (S.state === "playing") pos += (performance.now() - fetchedAt) / 1000;
  if (dur) pos = Math.min(pos, dur);
  if (!seeking) {
    const pct = dur ? (pos / dur) * 100 : 0;
    $("#fill").style.width = pct + "%";
    $("#knob").style.left = pct + "%";
    $("#pos").textContent = fmt(pos);
  }
  $("#dur").textContent = dur ? fmt(dur) : "–:––";
}

function renderQueue() {
  const q = S.queue;
  $("#queue-count").textContent = q.length || "";
  const total = q.reduce((a, t) => a + (t.duration || 0), 0);
  $("#queue-sum").textContent = q.length ? `${q.length} Titel · ${fmt(total)}` : "";
  $("#queue-empty").hidden = q.length > 0;
  const list = $("#queue");
  list.classList.add("sortable");
  const meShort = S.me.replace(/^@/, "").split(":")[0];
  list.replaceChildren(...q.map((t, i) => {
    const mine = t.requester === meShort;
    const who = t.requester === "autoplay" ? "✨ Autoplay" : (t.requester && !mine ? `von ${t.requester}` : "");
    const li = row({ ...t, mine }, subline(t, who), [
      button("list", "Zu deiner Playlist", (ev) => playlistMenu(ev, { key: t.key }), "hover-only"),
      i > 0 ? button("top", "Als Nächstes", () => act("/api/queue", { op: "top", index: i }), "hover-only") : "",
      button("remove", "Entfernen", () => act("/api/queue", { op: "remove", index: i })),
    ].filter(Boolean));
    li.draggable = true;
    li.dataset.index = i;
    return li;
  }));
  const hist = $("#history");
  $("#history-head").hidden = S.history.length === 0;
  hist.replaceChildren(...S.history.map((t) => row(t, subline(t), t.url ? [
    button("plus", "Wieder einreihen", () => act("/api/add", { query: t.url, mode: "end" }, "Eingereiht")),
  ] : [])));
}

// Ziehen & Ablegen in der Warteschlange
(() => {
  const list = $("#queue");
  let from = null;
  list.addEventListener("dragstart", (ev) => {
    const li = ev.target.closest(".row"); if (!li) return;
    from = +li.dataset.index; li.classList.add("dragging");
    ev.dataTransfer.effectAllowed = "move";
  });
  list.addEventListener("dragend", () => { from = null; $$(".row", list).forEach((r) => r.classList.remove("dragging", "drop-before")); });
  list.addEventListener("dragover", (ev) => {
    ev.preventDefault();
    $$(".row", list).forEach((r) => r.classList.remove("drop-before"));
    const li = ev.target.closest(".row"); if (li) li.classList.add("drop-before");
  });
  list.addEventListener("drop", (ev) => {
    ev.preventDefault();
    const li = ev.target.closest(".row");
    if (from === null || !li) return;
    let to = +li.dataset.index;
    if (to > from) to -= 1;
    if (to !== from) act("/api/queue", { op: "move", index: from, to });
  });
})();

// ---------------------------------------------------------------- Player-Steuerung
$$("[data-cmd]").forEach((b) => b.addEventListener("click", () => {
  const name = b.dataset.cmd;
  if (name === "clear" && !confirm("Warteschlange leeren?")) return;
  cmd(name);
}));

$("#playpause").addEventListener("click", () => {
  if (!S) return;
  if (S.state === "playing") { setPlayIcon(false); cmd("pause"); }
  else if (S.state === "paused") { setPlayIcon(true); cmd("resume"); }
  else if (S.queue.length) cmd("skip");
  else { toast("Die Warteschlange ist leer – such dir etwas aus."); selectTab("search"); }
});
$("#loop").addEventListener("click", (ev) => { ev.currentTarget.classList.toggle("on"); cmd("loop", S && S.loop ? "off" : "on"); });
$("#autoplay").addEventListener("click", (ev) => {
  ev.currentTarget.classList.toggle("on");
  cmd("autoplay", S && S.autoplay ? "off" : "on", S && S.autoplay ? "Autoplay aus" : "Autoplay an – danach kommt ähnliche Musik");
});
$("#video").addEventListener("click", (ev) => { ev.currentTarget.classList.toggle("on"); cmd("video", S && S.video ? "off" : "on"); });
$("#to-playlist").addEventListener("click", (ev) => { if (S && S.track) playlistMenu(ev, { key: S.track.key }); });
$("#fav").addEventListener("click", (ev) => {
  if (S && S.track) ev.currentTarget.classList.toggle("on");
  if (S && S.track) cmd("favkey", S.track.key, S.favs.includes(S.track.key) ? "Aus Favoriten entfernt" : "Zu Favoriten hinzugefügt");
});
const softStop = () => cmd("softstop", $("#softstop-len").value, "Wird ausgeblendet …");
$("#softstop").addEventListener("click", softStop);
$("#softstop2").addEventListener("click", softStop);

const vol = $("#volume");
vol.addEventListener("pointerdown", () => sliding.add("volume"));
vol.addEventListener("input", () => { paintRange(vol); $("#volume-val").textContent = vol.value; });
vol.addEventListener("change", async () => { await cmd("volume", vol.value); sliding.delete("volume"); });

// Spulen: klicken oder ziehen
(() => {
  const prog = $("#progress"), bar = $(".bar", prog);
  const frac = (ev) => { const r = bar.getBoundingClientRect(); return Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width)); };
  const show = (f) => {
    $("#fill").style.width = f * 100 + "%"; $("#knob").style.left = f * 100 + "%";
    $("#pos").textContent = fmt(f * (S && S.track ? S.track.duration : 0));
  };
  prog.addEventListener("pointerdown", (ev) => {
    if (!S || !S.track || !S.track.duration) return;
    seeking = true; prog.classList.add("drag"); prog.setPointerCapture(ev.pointerId); show(frac(ev));
  });
  prog.addEventListener("pointermove", (ev) => { if (seeking) show(frac(ev)); });
  prog.addEventListener("pointerup", async (ev) => {
    if (!seeking) return;
    const target = Math.floor(frac(ev) * S.track.duration);
    prog.classList.remove("drag");
    await cmd("seek", target);
    seeking = false;
  });
})();

// ---------------------------------------------------------------- Tabs
function selectTab(name) {
  $$(".tabs [role=tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  $$(".tab").forEach((t) => { t.hidden = t.id !== `tab-${name}`; });
  if (name === "library") loadLibrary();
  if (name === "search") $("#search-q").focus();
  try { localStorage.setItem("musikbot-tab", name); } catch { /* egal */ }
}
$$(".tabs [role=tab]").forEach((b) => b.addEventListener("click", () => selectTab(b.dataset.tab)));

// ---------------------------------------------------------------- Suche
$("#search-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const q = $("#search-q").value.trim();
  if (!q) return;
  const list = $("#results");
  if (/^https?:\/\//i.test(q)) {
    list.replaceChildren(row({ title: q }, "Link", searchActions(q)));
    return;
  }
  list.replaceChildren(Object.assign(document.createElement("p"), { className: "empty", textContent: "Suche …" }));
  try {
    const { results } = await api("/api/search?q=" + encodeURIComponent(q));
    if (!results.length) { list.replaceChildren(Object.assign(document.createElement("p"), { className: "empty", textContent: "Nichts gefunden." })); return; }
    list.replaceChildren(...results.map((r) => row(r, subline(r), searchActions(r.url))));
  } catch (e) {
    if (e.message !== "login") list.replaceChildren(Object.assign(document.createElement("p"), { className: "empty", textContent: e.message }));
  }
});
function searchActions(url) {
  const add = (mode, text) => () => act("/api/add", { query: url, mode }, text);
  return [
    button("play", "Jetzt spielen", add("now", "Kommt jetzt (wird geladen) …")),
    button("next", "Als Nächstes", add("next", "Als Nächstes eingereiht")),
    button("plus", "Hinten anstellen", add("end", "Eingereiht")),
    button("list", "Zu deiner Playlist", (ev) => playlistMenu(ev, { query: url })),
  ];
}

// ---------------------------------------------------------------- Meins (persoenlich)
const emptyNote = (text) => Object.assign(document.createElement("p"), { className: "empty", textContent: text });
const enqueueBtn = (t) => t.url ? button("plus", "Einreihen", () => act("/api/add", { query: t.url, mode: "end" }, "Eingereiht")) : "";
const openPlaylists = new Set();

async function playlistOp(body, okText) {
  try {
    LIB = await api("/api/playlist", body);
    if (okText) toast(okText);
    if (!$("#tab-library").hidden) renderLibrary();
    return true;
  } catch (e) {
    if (e.message !== "login") toast(e.message, true);
    return false;
  }
}

async function loadLibrary() {
  try { LIB = await api("/api/library"); } catch { return; }
  renderLibrary();
}

function renderLibrary() {
  const lib = LIB;
  if (!lib) return;
  const me = lib.me;
  $("#hello-avatar").textContent = (me.name || "?").slice(0, 1).toUpperCase();
  $("#hello-name").textContent = `Hallo, ${me.name}!`;
  const hours = Math.floor(me.seconds / 3600), mins = Math.round((me.seconds % 3600) / 60);
  $("#hello-stats").textContent = me.plays
    ? `${me.plays} Titel von dir gewünscht · ${hours ? hours + " h " : ""}${mins} min Musik`
    : "Noch nichts von dir gewünscht – leg los unter „Suche“.";

  const cards = $("#lib-actions");
  const card = (title, sub, onClick, on = false) => {
    const b = document.createElement("button");
    b.className = "action-card" + (on ? " on" : "");
    const t = document.createElement("b"); t.textContent = title;
    const s2 = document.createElement("span"); s2.textContent = sub;
    b.append(t, s2);
    b.addEventListener("click", onClick);
    return b;
  };
  const always = S && S.always;
  cards.replaceChildren(
    always
      ? card("24/7 ausschalten", "Dauerschleife beenden", () => cmd("always", "off", "24/7 ist aus"), true)
      : card("24/7 Radio", lib.radio ? `${lib.radio} Dateien, zufällig` : "Radio-Ordner ist leer", () => cmd("always", "radio", "Radio läuft")),
    card("Deine Favoriten", `${lib.favorites.length} Titel gemischt abspielen`, () => cmd("fav", "play shuffle")),
  );
  $("#fav-play").onclick = () => cmd("fav", "play");

  // Eigene Playlists: aufklappbar, mit Titeln
  const box = $("#my-playlists");
  box.replaceChildren(...(lib.my_playlists.length ? lib.my_playlists.map((p) => {
    const wrap = document.createElement("div");
    wrap.className = "pl";
    const head = document.createElement("div");
    head.className = "pl-head";
    const name = document.createElement("b"); name.textContent = p.name;
    const count = document.createElement("span"); count.className = "muted"; count.textContent = `${p.count} Titel`;
    head.append(name, count,
      button("play", "Abspielen", () => cmd("playlist", `play ${p.name}`)),
      button("shuffle", "Gemischt abspielen", () => cmd("playlist", `play ${p.name} shuffle`)),
      button("plus", "Einreihen", () => cmd("playlist", `enqueue ${p.name}`, "Eingereiht")),
      button("trash", "Playlist löschen", () => {
        if (confirm(`Playlist „${p.name}“ löschen?`)) playlistOp({ op: "delete", name: p.name }, "Gelöscht");
      }, "hover-only"));
    head.addEventListener("click", () => {
      openPlaylists.has(p.name) ? openPlaylists.delete(p.name) : openPlaylists.add(p.name);
      renderLibrary();
    });
    wrap.append(head);
    if (openPlaylists.has(p.name)) {
      const list = document.createElement("ol");
      list.className = "list";
      list.replaceChildren(...(p.tracks.length ? p.tracks.map((t, i) => row(t, subline(t), [
        enqueueBtn(t),
        button("remove", "Aus Playlist entfernen", () => playlistOp({ op: "remove", name: p.name, index: i })),
      ].filter(Boolean))) : [emptyNote("Leer – füge Titel über das Listen-Symbol beim Player, in der Suche oder Warteschlange hinzu.")]));
      wrap.append(list);
    }
    return wrap;
  }) : [emptyNote("Noch keine eigenen Playlists. Leg oben eine an.")]));

  $("#favorites").replaceChildren(...(lib.favorites.length ? lib.favorites.map((t) => row(t, subline(t), [
    enqueueBtn(t),
    button("list", "Zu deiner Playlist", (ev) => playlistMenu(ev, { key: t.key }), "hover-only"),
    button("remove", "Aus Favoriten entfernen", async () => { await cmd("favkey", t.key); loadLibrary(); }, "hover-only"),
  ].filter(Boolean))) : [emptyNote("Noch keine Favoriten – tipp beim laufenden Titel aufs Herz.")]));

  const simple = (items, extra) => items.map((t) => row(t, subline(t, extra ? extra(t) : ""), [
    enqueueBtn(t),
    button("list", "Zu deiner Playlist", (ev) => playlistMenu(ev, { key: t.key }), "hover-only"),
  ].filter(Boolean)));
  $("#my-recent").replaceChildren(...(lib.my_recent.length ? simple(lib.my_recent) : [emptyNote("Noch nichts.")]));
  $("#my-top").replaceChildren(...(lib.my_top.length ? simple(lib.my_top, (t) => `${t.plays}×`) : [emptyNote("Noch nichts.")]));

  $("#others-head").hidden = lib.playlists.length === 0;
  $("#playlists").replaceChildren(...lib.playlists.map((p) =>
    row({ title: p.name }, [p.by ? `von ${p.by}` : "", `${p.count} Titel`].filter(Boolean).join(" · "), [
      button("play", "Abspielen", () => cmd("playlist", `play ${p.name}`)),
      button("shuffle", "Gemischt abspielen", () => cmd("playlist", `play ${p.name} shuffle`)),
      button("plus", "Einreihen", () => cmd("playlist", `enqueue ${p.name}`, "Eingereiht")),
    ])));
  $("#top").replaceChildren(...simple(lib.top, (t) => `${t.plays}× gespielt`));
}

$("#pl-new").addEventListener("click", () => {
  const f = $("#pl-form");
  f.hidden = !f.hidden;
  if (!f.hidden) $("#pl-name").focus();
});
$("#pl-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const name = $("#pl-name").value.trim();
  if (!name) return;
  if (await playlistOp({ op: "create", name }, `Playlist „${name}“ angelegt`)) {
    $("#pl-name").value = "";
    $("#pl-form").hidden = true;
  }
});

// Kleines Menue "Zu deiner Playlist" (eigene Playlists + neue anlegen)
async function playlistMenu(ev, item) {
  ev.stopPropagation();
  const anchor = ev.currentTarget;              // nach dem await ist currentTarget weg
  if (!LIB) await loadLibrary();
  if (!LIB) return;
  const pop = $("#popover");
  const title = Object.assign(document.createElement("div"), { className: "pop-title", textContent: "Zu Playlist hinzufügen" });
  const entries = LIB.my_playlists.map((p) => {
    const b = Object.assign(document.createElement("button"), { textContent: p.name });
    b.addEventListener("click", () => { closePopover(); playlistOp({ op: "add", name: p.name, ...item }, `Zu „${p.name}“ hinzugefügt`); });
    return b;
  });
  const form = document.createElement("form");
  form.className = "inline-form";
  form.style.margin = "4px";
  const input = Object.assign(document.createElement("input"), { placeholder: "Neue Playlist …", maxLength: 60 });
  const ok = Object.assign(document.createElement("button"), { className: "btn primary", type: "submit", textContent: "+" });
  form.append(input, ok);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = input.value.trim();
    if (!name) return;
    closePopover();
    if (await playlistOp({ op: "create", name })) playlistOp({ op: "add", name, ...item }, `„${name}“ angelegt und Titel hinzugefügt`);
  });
  pop.replaceChildren(title, ...entries, form);
  pop.hidden = false;
  const r = anchor.getBoundingClientRect();
  const w = pop.offsetWidth, h = pop.offsetHeight;
  pop.style.left = Math.max(16, Math.min(window.innerWidth - w - 16, r.right - w)) + "px";
  pop.style.top = (r.bottom + h + 8 > window.innerHeight ? Math.max(16, r.top - h - 6) : r.bottom + 6) + "px";
  if (!entries.length) input.focus();
}
function closePopover() { $("#popover").hidden = true; }
document.addEventListener("click", (ev) => { if (!ev.target.closest("#popover")) closePopover(); });
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") closePopover(); });

// ---------------------------------------------------------------- Fades
function drawCurve(seconds) {
  // Equal-Power: alt = cos, neu = sin; die Breite zeigt die Laenge (max. 20 s)
  const w = 300, h = 80, pad = 4;
  const span = Math.max(0.02, seconds / 20) * w;
  const x0 = (w - span) / 2;
  let out = `M0 ${pad} L${x0} ${pad}`, inn = `M0 ${h - pad} L${x0} ${h - pad}`;
  for (let i = 0; i <= 40; i++) {
    const t = i / 40, x = x0 + t * span;
    out += ` L${x.toFixed(1)} ${(pad + (1 - Math.cos(t * Math.PI / 2)) * (h - 2 * pad)).toFixed(1)}`;
    inn += ` L${x.toFixed(1)} ${(h - pad - Math.sin(t * Math.PI / 2) * (h - 2 * pad)).toFixed(1)}`;
  }
  $("#curve-out").setAttribute("d", out + ` L${w} ${h - pad}`);
  $("#curve-in").setAttribute("d", inn + ` L${w} ${pad}`);
}
function renderFades() {
  setRange($("#crossfade"), S.crossfade);
  setRange($("#fadein"), S.fade_in);
  if (!sliding.has("crossfade")) { $("#cf-val").textContent = S.crossfade ? `${S.crossfade} s` : "aus"; drawCurve(S.crossfade); }
  if (!sliding.has("fadein")) $("#fi-val").textContent = S.fade_in ? `${S.fade_in} s` : "aus";
}
[["crossfade", "cf-val", "crossfade"], ["fadein", "fi-val", "fade_in"]].forEach(([id, label, field]) => {
  const el = $("#" + id);
  el.addEventListener("pointerdown", () => sliding.add(id));
  el.addEventListener("input", () => {
    sliding.add(id); paintRange(el);
    $("#" + label).textContent = +el.value ? `${el.value} s` : "aus";
    if (id === "crossfade") drawCurve(+el.value);
  });
  el.addEventListener("change", async () => { await act("/api/fade", { [field]: +el.value }); sliding.delete(id); });
});
const ss = $("#softstop-len");
paintRange(ss);
ss.addEventListener("input", () => { paintRange(ss); $("#ss-val").textContent = `${ss.value} s`; });

// ---------------------------------------------------------------- Start
async function poll() {
  clearTimeout(pollTimer);
  try {
    apply(await api("/api/state"));
  } catch (e) {
    if (e.message === "login") return;
  }
  pollTimer = setTimeout(poll, document.hidden ? 5000 : 1000);
}
document.addEventListener("visibilitychange", () => { if (!document.hidden && !$("#app").hidden) poll(); });

async function start() {
  try {
    await linkLogin();
    apply(await api("/api/state"));
  } catch (e) {
    if (e.message !== "login" && $("#login").hidden) showLogin("Server nicht erreichbar – gleich nochmal versuchen.");
    return;
  }
  $("#login").hidden = true;
  $("#app").hidden = false;
  let tab = "queue";
  try { tab = localStorage.getItem("musikbot-tab") || tab; } catch { /* egal */ }
  selectTab(tab === "search" ? "queue" : tab);
  if (tab !== "library") loadLibrary();
  poll();
  if (!start.ticking) { start.ticking = true; setInterval(tick, 250); }
}
start();
