"use strict";
/* JobHunt dashboard (public, read-only: lists every job found) — dependency-free. All user/job text is inserted via
   textContent (never innerHTML) so scraped content cannot inject markup. */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "text") el.textContent = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } },
};

// ------------------------------------------------------------------ state
// Everything found is shown by default; the toggles only narrow the list.
const DEFAULTS = {
  q: "", skills: [], skills_mode: "all", exclude_skills: [], seniority: [], source: [],
  company: [], location: "", remote: "", max_years: "", posted_days: "", status: "active",
  has_salary: false, include_duplicates: false,
  tech_only: false, include_flagged: true, include_lang_restricted: true, include_indirect: true, category: [],
  sort: "posted", order: "desc", page: 1, page_size: 50,
};
const ARRAY_KEYS = ["skills", "exclude_skills", "seniority", "source", "company", "category"];
const BOOL_KEYS = ["has_salary", "include_duplicates", "tech_only", "include_flagged", "include_lang_restricted", "include_indirect"];

let S = loadState();
let lastData = null;
let openId = null;
let lastOpenCount = null;
let reqSeq = 0;

function loadState() {
  const p = new URLSearchParams(location.hash.slice(1));
  const s = structuredClone(DEFAULTS);
  s.page_size = store.get("page_size", DEFAULTS.page_size);
  for (const k of Object.keys(DEFAULTS)) {
    if (!p.has(k)) continue;
    if (ARRAY_KEYS.includes(k)) s[k] = p.getAll(k);
    else if (BOOL_KEYS.includes(k)) s[k] = p.get(k) === "true";
    else if (k === "page" || k === "page_size") s[k] = Math.max(1, parseInt(p.get(k), 10) || DEFAULTS[k]);
    else s[k] = p.get(k);
  }
  return s;
}

function toParams(s, { forApi = false } = {}) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(s)) {
    if (!forApi && JSON.stringify(v) === JSON.stringify(DEFAULTS[k])) continue;
    if (Array.isArray(v)) v.forEach((x) => p.append(k, x));
    else if (typeof v === "boolean") p.set(k, String(v));
    else if (v !== "" && v !== null && v !== undefined) p.set(k, String(v));
  }
  return p;
}

function setState(patch, { resetPage = true } = {}) {
  S = { ...S, ...patch };
  if (resetPage && !("page" in patch)) S.page = 1;
  history.replaceState(null, "", "#" + toParams(S).toString());
  syncControls();
  loadJobs();
}

// ------------------------------------------------------------------ api
async function api(path, opts = {}) {
  const res = await fetch("/api" + path, {
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try { const j = await res.json(); if (j.detail) msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch { /* ignore */ }
    throw new Error(msg);
  }
  return res.json();
}

// ------------------------------------------------------------------ formatting
const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
function ago(iso) {
  if (!iso) return "—";
  const sec = (new Date(iso) - Date.now()) / 1000;
  const abs = Math.abs(sec);
  if (abs < 60) return rtf.format(Math.round(sec), "second");
  if (abs < 3600) return rtf.format(Math.round(sec / 60), "minute");
  if (abs < 86400) return rtf.format(Math.round(sec / 3600), "hour");
  if (abs < 86400 * 45) return rtf.format(Math.round(sec / 86400), "day");
  return new Date(iso).toLocaleDateString();
}
function money(n, cur) {
  if (!n) return "";
  const k = n >= 1000 ? `${Math.round(n / 1000)}k` : String(n);
  return (cur === "USD" || !cur ? "$" : cur === "EUR" ? "€" : cur === "GBP" ? "£" : cur + " ") + k;
}
function salary(j) {
  if (!j.salary_min && !j.salary_max) return "";
  if (j.salary_min && j.salary_max && j.salary_min !== j.salary_max) return `${money(j.salary_min, j.salary_currency)}–${money(j.salary_max, j.salary_currency).replace(/^\D+/, "")}`;
  return money(j.salary_max || j.salary_min, j.salary_currency);
}
const titleCase = (s) => s ? s[0].toUpperCase() + s.slice(1) : s;

// ------------------------------------------------------------------ toasts
function toast(msg, { action, onAction, error = false, ms = 5000 } = {}) {
  const el = h("div", { class: "toast" + (error ? " err" : "") }, h("span", { text: msg }));
  if (action) el.append(h("button", { text: action, onclick: () => { el.remove(); onAction(); } }));
  $("#toasts").append(el);
  setTimeout(() => el.remove(), ms);
}

// ------------------------------------------------------------------ jobs table
async function loadJobs({ quiet = false } = {}) {
  const seq = ++reqSeq;
  if (!quiet) $("#jobs").classList.add("loading");
  try {
    const data = await api("/jobs?" + toParams(S, { forApi: true }).toString());
    if (seq !== reqSeq) return; // a newer request superseded this one
    lastData = data;
    if (data.items.length === 0 && S.page > 1 && data.total > 0) { setState({ page: 1 }); return; }
    renderJobs(data);
  } catch (e) {
    if (seq === reqSeq) toast("Failed to load jobs: " + e.message, { error: true });
  } finally {
    if (seq === reqSeq) $("#jobs").classList.remove("loading");
  }
}

function renderJobs(data) {
  const tbody = $("#rows");
  tbody.replaceChildren();
  for (const j of data.items) {
    tbody.append(jobRow(j));
    if (j.id === openId) tbody.append(detailRow(j));
  }
  const from = data.total ? (data.page - 1) * data.page_size + 1 : 0;
  $("#result-count").replaceChildren(h("b", { text: data.total.toLocaleString() }), ` job${data.total === 1 ? "" : "s"}`,
    data.total ? ` · showing ${from}–${from + data.items.length - 1}` : "");
  const empty = $("#empty");
  empty.classList.toggle("hidden", data.items.length > 0);
  if (!data.items.length) {
    empty.textContent = !hasFilters()
      ? "No jobs yet. The first fetch is running — this page fills up within a few minutes."
      : "No jobs match these filters.";
  }
  renderPager(data);
  $$("#jobs th[data-sort]").forEach((th) => {
    th.classList.toggle("sorted", th.dataset.sort === S.sort);
    th.classList.toggle("asc", th.dataset.sort === S.sort && S.order === "asc");
  });
}

function hasFilters() {
  return ["q", "skills", "exclude_skills", "seniority", "source", "company", "location", "remote", "max_years",
    "posted_days", "has_salary", "category", "tech_only", "include_flagged", "include_lang_restricted", "include_indirect"].some((k) => JSON.stringify(S[k]) !== JSON.stringify(DEFAULTS[k]));
}

function skillChips(skills) {
  const sorted = [...skills].sort((a, b) => S.skills.includes(b) - S.skills.includes(a));
  const shown = sorted.slice(0, 3);
  const wrap = h("div", { class: "skills" }, shown.map((s) =>
    h("span", { class: "sk" + (S.skills.includes(s) ? " hit" : ""), text: s })));
  if (sorted.length > shown.length) wrap.append(h("span", { class: "sk more", text: `+${sorted.length - shown.length}`, title: sorted.slice(3).join(", ") }));
  return wrap;
}

function jobRow(j) {
  const badges = [
    j.remote && h("span", { class: "badge remote", text: "remote" }),
    j.status === "closed" && h("span", { class: "badge closed", text: "closed", title: j.check_result || "" }),
    j.duplicate_of && h("span", { class: "badge dup", text: "duplicate" }),
    !j.is_tech && h("span", { class: "badge cat", text: j.category }),
    !j.trusted && h("span", { class: "badge flag", text: "flagged", title: (j.trust_flags || []).join("\n") }),
    (j.languages || []).length > 0 && h("span", { class: "badge lang", text: j.languages.join(", ") + " required" }),
    !(j.languages || []).length && !["english", "unknown"].includes(j.text_lang) && h("span", { class: "badge lang", text: `${j.text_lang} JD` }),
    j.link_rank >= 3 && h("span", { class: "badge flag", text: "no direct link", title: "Only the aggregator's page is known; the employer's own posting hasn't been found yet" }),
  ];
  const tr = h("tr", { class: "job" + (j.status === "closed" ? " closed" : "") + (j.id === openId ? " open" : ""), dataset: { id: j.id } },
    h("td", { class: "title-cell" },
      h("div", { class: "title", text: j.title }),
      h("div", { class: "sub" }, [j.source, j.department, j.employment_type].filter(Boolean).join(" · "), " ", badges)),
    h("td", { class: "company", text: j.company, title: j.company }),
    h("td", { class: "loc", title: j.location || "" }, h("span", { text: j.location || "—" })),
    h("td", {}, skillChips(j.skills || [])),
    h("td", { class: "num", text: titleCase(j.seniority) + (j.years_required ? ` · ${j.years_required}+ yrs` : ""), title: j.years_required ? `${j.years_required}+ years of experience requested` : "" }),
    h("td", { class: "num", text: salary(j) || "—" }),
    h("td", { class: "num", text: ago(j.posted_at || j.first_seen_at), title: `posted ${j.posted_at || "unknown"}\nfirst seen ${j.first_seen_at}` }),
    h("td", { class: "actions-cell" }, h("div", { class: "actions" },
      h("a", { class: "btn small", href: j.apply_url || j.url, target: "_blank", rel: "noopener noreferrer", text: "Open" }))),
  );
  tr.addEventListener("click", (e) => {
    if (e.target.closest("button, a, input, textarea")) return;
    openId = openId === j.id ? null : j.id;
    renderJobs(lastData);
  });
  return tr;
}

function detailRow(j) {
  const td = h("td", { colspan: 8 });
  const tr = h("tr", { class: "detail" }, td);
  const desc = h("div", { class: "desc", text: "Loading…" });
  const checkLine = h("dd", { text: j.last_checked_at ? `${j.check_result || "ok"} (${ago(j.last_checked_at)})` : (j.authoritative ? "tracked via company board feed" : "not checked yet") });
  const dups = h("dd", { text: "—" });
  td.append(h("div", { class: "detail-grid" }, desc, h("dl", { class: "meta" },
    h("dt", { text: "Links" }), h("dd", {},
      h("a", { href: j.url, target: "_blank", rel: "noopener noreferrer", text: "Posting" }),
      j.apply_url && j.apply_url !== j.url ? [" · ", h("a", { href: j.apply_url, target: "_blank", rel: "noopener noreferrer", text: "Apply form" })] : null),
    h("dt", { text: "Posting status" }), h("dd", {}, h("b", { text: j.status })), checkLine,
    h("dt", { text: "Category" }), h("dd", { text: `${j.category}${j.is_tech ? "" : " (non-tech)"}` }),
    h("dt", { text: "Apply link" }), h("dd", { text: LINK_RANK[j.link_rank] || "—" }),
    (j.trust_flags || []).length ? [h("dt", { text: "Trust warnings" }), h("dd", {}, h("ul", { class: "flags" }, j.trust_flags.map((f) => h("li", { text: f }))))] : null,
    (j.languages || []).length ? [h("dt", { text: "Language requirement" }), h("dd", { text: j.languages.join(", ") })] : null,
    h("dt", { text: "All skills" }), h("dd", { text: (j.skills || []).join(", ") || "—" }),
    h("dt", { text: "Seen" }), h("dd", { text: `first ${ago(j.first_seen_at)}, last ${ago(j.last_seen_at)}` }),
    h("dt", { text: "Also listed on" }), dups,
  )));
  api(`/jobs/${j.id}`).then((full) => {
    desc.textContent = full.description || "(no description provided by this source — open the posting)";
    if (full.duplicates.length) dups.replaceChildren(...full.duplicates.flatMap((d, i) => [i ? ", " : "", h("a", { href: d.url, target: "_blank", rel: "noopener noreferrer", text: d.source })]));
  }).catch((e) => { desc.textContent = "Failed to load: " + e.message; });
  return tr;
}

function renderPager(data) {
  const pages = Math.max(1, Math.ceil(data.total / data.page_size));
  const sizeSel = h("select", { "aria-label": "Rows per page", onchange: (e) => { store.set("page_size", +e.target.value); setState({ page_size: +e.target.value }); } },
    [25, 50, 100, 200].map((n) => h("option", { value: n, selected: n === data.page_size, text: `${n} / page` })));
  $("#pager").replaceChildren(
    h("div", {}, sizeSel),
    h("div", { class: "pages" },
      h("button", { class: "btn small ghost", text: "‹ Prev", disabled: data.page <= 1, onclick: () => setState({ page: data.page - 1 }, { resetPage: false }) }),
      h("span", { text: `Page ${data.page} of ${pages}` }),
      h("button", { class: "btn small ghost", text: "Next ›", disabled: data.page >= pages, onclick: () => setState({ page: data.page + 1 }, { resetPage: false }) }),
    ),
  );
}

// ------------------------------------------------------------------ filters
function chipList(container, values, key, cls = "") {
  container.replaceChildren(...values.map((v) => h("span", { class: "chip " + cls }, v,
    h("button", { "aria-label": `Remove ${v}`, text: "×", onclick: () => setState({ [key]: S[key].filter((x) => x !== v) }) }))));
}

function addFromInput(input, key, allowed) {
  const raw = input.value.trim();
  if (!raw) return;
  const match = allowed ? allowed.find((a) => a.toLowerCase() === raw.toLowerCase()) : raw;
  if (!match) { toast(`Unknown: ${raw}`, { error: true, ms: 2500 }); return; }
  input.value = "";
  if (!S[key].includes(match)) setState({ [key]: [...S[key], match] });
}

let facets = { skills: [], sources: [], seniority: [], companies: [], seniority_levels: [], categories: [] };
const LINK_RANK = {
  0: "employer's own hiring system (best)",
  1: "aggregator listing that links to the employer's hiring system",
  2: "employer careers site via aggregator",
  3: "aggregator page only — employer posting not found yet (may require an aggregator account)",
};
function currentTheme() {
  return document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}
function syncThemeSwitch() { $("#btn-theme").setAttribute("aria-checked", String(currentTheme() === "dark")); }
let knownSkills = [];

function renderFacets() {
  const counts = (arr) => Object.fromEntries(arr.map((x) => [x.value, x.count]));
  const sen = counts(facets.seniority);
  $("#f-seniority").replaceChildren(...facets.seniority_levels.map((lvl) => h("label", {},
    h("input", { type: "checkbox", checked: S.seniority.includes(lvl), onchange: (e) => setState({ seniority: e.target.checked ? [...S.seniority, lvl] : S.seniority.filter((x) => x !== lvl) }) }),
    titleCase(lvl), h("span", { class: "n", text: sen[lvl] || 0 }))));
  const srcCounts = counts(facets.sources);
  const srcNames = [...new Set([...facets.sources.map((s) => s.value), ...S.source])];
  $("#f-sources").replaceChildren(...srcNames.map((src) => h("label", {},
    h("input", { type: "checkbox", checked: S.source.includes(src), onchange: (e) => setState({ source: e.target.checked ? [...S.source, src] : S.source.filter((x) => x !== src) }) }),
    src, h("span", { class: "n", text: srcCounts[src] || 0 }))));
  const catCounts = counts(facets.categories || []);
  const cats = [...new Set([...(facets.categories || []).map((c) => c.value), ...S.category])];
  $("#f-categories").replaceChildren(...cats.map((c) => h("label", {},
    h("input", { type: "checkbox", checked: S.category.includes(c), onchange: (e) => setState({ category: e.target.checked ? [...S.category, c] : S.category.filter((x) => x !== c), ...(e.target.checked ? { tech_only: false } : {}) }) }),
    c, h("span", { class: "n", text: catCounts[c] || 0 }))));
  $("#company-list").replaceChildren(...facets.companies.map((c) => h("option", { value: c.value, text: `${c.count} jobs` })));
  const skillSet = [...new Set([...facets.skills.map((s) => s.value), ...knownSkills])];
  $("#skill-list").replaceChildren(...skillSet.map((s) => h("option", { value: s })));
  $("#top-skills").replaceChildren(...facets.skills.filter((s) => !S.skills.includes(s.value)).slice(0, 18).map((s) =>
    h("button", { title: `Filter by ${s.value}`, onclick: () => setState({ skills: [...S.skills, s.value] }) }, s.value, h("span", { class: "n", text: s.count }))));
}

function syncControls() {
  if (document.activeElement !== $("#f-q")) $("#f-q").value = S.q;
  if (document.activeElement !== $("#f-location")) $("#f-location").value = S.location;
  $("#f-max-years").value = S.max_years;
  $("#f-posted").value = S.posted_days;
  $("#f-status").value = S.status;
  $("#f-salary").checked = S.has_salary;
  $("#f-dups").checked = S.include_duplicates;
  $("#f-flagged").checked = !S.include_flagged;
  $("#f-lang").checked = !S.include_lang_restricted;
  $("#f-indirect").checked = !S.include_indirect;
  $$("#f-skills-mode button").forEach((b) => b.classList.toggle("on", b.dataset.v === S.skills_mode));
  $$("#f-remote button").forEach((b) => b.classList.toggle("on", b.dataset.v === S.remote));
  chipList($("#f-skills"), S.skills, "skills");
  chipList($("#f-xskills"), S.exclude_skills, "exclude_skills", "x");
  chipList($("#f-companies"), S.company, "company");
  renderFacets();
}

function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

function bindControls() {
  $("#f-q").addEventListener("input", debounce((e) => setState({ q: e.target.value }), 300));
  $("#f-location").addEventListener("input", debounce((e) => setState({ location: e.target.value.trim() }), 300));
  $("#f-max-years").addEventListener("change", (e) => setState({ max_years: e.target.value }));
  $("#f-posted").addEventListener("change", (e) => setState({ posted_days: e.target.value }));
  $("#f-status").addEventListener("change", (e) => setState({ status: e.target.value }));
  $("#f-salary").addEventListener("change", (e) => setState({ has_salary: e.target.checked }));
  $("#f-dups").addEventListener("change", (e) => setState({ include_duplicates: e.target.checked }));
  $("#f-flagged").addEventListener("change", (e) => setState({ include_flagged: !e.target.checked }));
  $("#f-lang").addEventListener("change", (e) => setState({ include_lang_restricted: !e.target.checked }));
  $("#f-indirect").addEventListener("change", (e) => setState({ include_indirect: !e.target.checked }));
  $$("#f-skills-mode button").forEach((b) => b.addEventListener("click", () => setState({ skills_mode: b.dataset.v })));
  $$("#f-remote button").forEach((b) => b.addEventListener("click", () => setState({ remote: b.dataset.v })));
  const skillInput = (inp, key) => {
    inp.addEventListener("change", () => addFromInput(inp, key, knownSkills));
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") addFromInput(inp, key, knownSkills); });
  };
  skillInput($("#f-skill-input"), "skills");
  skillInput($("#f-xskill-input"), "exclude_skills");
  const comp = $("#f-company-input");
  comp.addEventListener("change", () => addFromInput(comp, "company", null));
  comp.addEventListener("keydown", (e) => { if (e.key === "Enter") addFromInput(comp, "company", null); });
  $("#btn-reset").addEventListener("click", () => {
    const keep = { sort: S.sort, order: S.order, page_size: S.page_size, status: S.status };
    setState({ ...structuredClone(DEFAULTS), ...keep });
  });
  $$("#jobs th[data-sort]").forEach((th) => th.addEventListener("click", () => {
    const key = th.dataset.sort;
    const textual = ["title", "company", "location", "source", "seniority"].includes(key);
    setState({ sort: key, order: S.sort === key ? (S.order === "desc" ? "asc" : "desc") : (textual ? "asc" : "desc") });
  }));
  $("#btn-status").addEventListener("click", openDrawer);
  $("#drawer-close").addEventListener("click", () => $("#drawer").classList.add("hidden"));
  $("#btn-filters").addEventListener("click", () => $("#filters").classList.toggle("show"));
  $("#btn-theme").addEventListener("click", () => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    store.set("theme", next);
    syncThemeSwitch();
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", syncThemeSwitch);
  $("#newjobs").addEventListener("click", () => { $("#newjobs").classList.add("hidden"); refreshAll(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { $("#drawer").classList.add("hidden"); if (openId) { openId = null; renderJobs(lastData); } }
    if (e.key === "/" && !e.target.closest("input, textarea, select")) { e.preventDefault(); $("#f-q").focus(); }
  });
  window.addEventListener("hashchange", () => { S = loadState(); syncControls(); loadJobs(); });
}

// ------------------------------------------------------------------ stats, status, facets
async function loadStats() {
  try {
    const s = await api("/stats");
    const n = (x) => (x ?? 0).toLocaleString();
    $("#stats").replaceChildren(
      h("span", { class: "stat", title: "Open postings (duplicates collapsed)" }, h("b", { text: n(s.open_jobs) }), " open jobs at ", h("b", { text: n(s.open_companies) }), " companies"),
      h("span", { class: "stat" }, h("b", { text: n(s.new_24h) }), " new in 24h"),
      h("span", { class: "stat alt" }, h("b", { text: n(s.open_remote_jobs) }), " remote"),
      h("span", { class: "stat alt", title: `${n(s.companies_hiring)} currently hiring · ${n(s.companies_tech)} with tech roles · ${n(s.companies_pending)} not fetched yet` },
        h("b", { text: n(s.companies) }), " companies tracked"),
    );
    $$("[data-count]").forEach((el) => { el.textContent = (s[el.dataset.count] ?? "").toLocaleString(); });
    if (lastOpenCount !== null && s.new_24h > lastOpenCount) {
      const el = $("#newjobs");
      el.textContent = `${s.new_24h - lastOpenCount} new — click to load`;
      el.classList.remove("hidden");
    }
    lastOpenCount = s.new_24h;
  } catch { /* transient */ }
}

let lastStatus = null;
async function loadStatus() {
  try {
    const st = await api("/status");
    lastStatus = st;
    const el = $("#sync");
    const failed = st.boards.filter((b) => b.ok === false).length;
    const due = (st.registry || []).reduce((a, r) => a + (r.due || 0), 0);
    el.className = "sync " + (st.fetching ? "busy" : failed ? "err" : "ok");
    $(".label", el).textContent = st.fetching ? `fetching… ${due ? due.toLocaleString() + " boards due" : ""}` :
      st.last_fetch_finished ? `updated ${ago(st.last_fetch_finished)}${failed ? ` · ${failed} source${failed > 1 ? "s" : ""} failing` : ""}` : "waiting for first fetch";
    el.title = st.next_fetch_at ? `Next fetch ${ago(st.next_fetch_at)}` : "";
    const banner = $("#config-error");
    banner.textContent = st.config_error ? `Config problem — previous config still active:\n${st.config_error}` : "";
    banner.classList.toggle("hidden", !st.config_error);
    if (!$("#drawer").classList.contains("hidden")) renderDrawer(st);
    return st;
  } catch { $("#sync").className = "sync err"; $("#sync .label").textContent = "backend unreachable"; }
}

async function loadFacets() {
  try {
    facets = await api("/facets");
    renderFacets();
  } catch { /* transient */ }
}

async function loadConfig() {
  try {
    const c = await api("/config");
    knownSkills = c.known_skills;
  } catch { /* ignore */ }
}

async function openDrawer() {
  $("#drawer").classList.remove("hidden");
  $("#drawer-body").textContent = "Loading…";
  renderDrawer(await loadStatus() || lastStatus);
}

function renderDrawer(st) {
  if (!st) return;
  const kv = (k, v) => [h("span", { text: k }), h("span", { text: v })];
  const num = (x) => (x ?? 0).toLocaleString();
  const reg = st.registry || [];
  const sum = (k) => reg.reduce((a, r) => a + (r[k] || 0), 0);
  const body = $("#drawer-body");
  const children = [
    h("div", { class: "kv" },
      kv("Board fetches", `${num(st.boards_done)} since start · ${st.boards_in_flight} in flight`),
      kv("Aggregators", st.next_fetch_at ? `next cycle ${ago(st.next_fetch_at)}` : "—"),
      kv("Last liveness check", st.last_validation_at ? ago(st.last_validation_at) : "—"),
      kv("Last dedup / cleanup", st.last_maintenance_at ? ago(st.last_maintenance_at) : "—"),
      st.storage ? kv("Database", `${num(st.storage.db_mb)} MB${st.storage.budget_mb ? ` of ${num(st.storage.budget_mb)} MB budget` : ""}`
        + (st.storage.job_cap ? ` · keeping the newest ${num(st.storage.job_cap)} jobs` : "")) : null,
      kv("Config version", String(st.config_version)),
      st.last_error ? kv("Last error", st.last_error) : null,
    ),
    h("h3", { text: `Company registry — ${num(sum("total"))} boards` }),
    h("table", {},
      h("thead", {}, h("tr", {}, ["ATS", "Boards", "Hiring", "Tech roles", "Empty", "Failing", "Retired", "Pending", "Due"].map((t) => h("th", { text: t })))),
      h("tbody", {}, reg.map((r) => h("tr", {},
        h("td", { text: r.ats }), ...["total", "with_jobs", "with_tech", "empty", "failing", "gone", "pending", "due"].map((k) => h("td", { class: "num", text: num(r[k]) })),
      ))),
    ),
    h("h3", { text: "Aggregators" }),
    h("table", {},
      h("thead", {}, h("tr", {}, ["Feed", "Last run", "Fetched", "New", "Result"].map((t) => h("th", { text: t })))),
      h("tbody", {}, st.boards.map((b) => h("tr", {},
        h("td", { text: b.board }),
        h("td", { text: b.finished_at ? ago(b.finished_at) : "pending" }),
        h("td", { class: "num", text: b.unchanged ? "unchanged" : (b.fetched ?? "—") }),
        h("td", { class: "num", text: b.new ?? "—" }),
        h("td", {}, b.ok ? (b.error ? h("span", { class: "err", text: "partial: " + b.error }) : "ok") : h("span", { class: "err", text: b.error || "failed" })),
      ))),
    ),
    (st.failing_boards || []).length ? [
      h("h3", { text: "Failing boards (backing off)" }),
      h("table", {}, h("tbody", {}, st.failing_boards.map((f) => h("tr", {},
        h("td", { text: `${f.ats}:${f.token}` }), h("td", { text: f.name }), h("td", { class: "err", text: `${f.fail_count}× ${f.last_error}` }))))),
    ] : null,
  ];
  body.replaceChildren(...children.flat().filter(Boolean));
}

function refreshAll() { loadJobs({ quiet: true }); loadStats(); loadFacets(); }

// ------------------------------------------------------------------ boot
(async function boot() {
  const theme = store.get("theme", null);
  if (theme) document.documentElement.dataset.theme = theme;
  syncThemeSwitch();
  bindControls();
  syncControls();
  await loadConfig();
  loadJobs();
  loadStats();
  loadFacets();
  loadStatus();
  setInterval(loadStatus, 15000);
  setInterval(loadStats, 30000);
  setInterval(() => { if (!document.hidden) loadFacets(); }, 120000);
})();
