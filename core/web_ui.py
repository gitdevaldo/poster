from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from core.account_manager import (
    add_account,
    delete_account,
    get_active_account,
    list_accounts,
    list_groups_for_account,
    load_raw_config,
    set_account_enabled,
    set_active_account,
    set_group_included,
)
from core.config_loader import ACCOUNT_ENV_VAR
from core.group_scraper import scrape_groups
from core.scheduler import run_scheduler
from core.session_manager import ensure_session, validate_session


@contextmanager
def _account_env(account_id: str | None):
    previous = os.environ.get(ACCOUNT_ENV_VAR)
    try:
        if account_id:
            os.environ[ACCOUNT_ENV_VAR] = account_id
        elif ACCOUNT_ENV_VAR in os.environ:
            del os.environ[ACCOUNT_ENV_VAR]
        yield
    finally:
        if previous is None:
            if ACCOUNT_ENV_VAR in os.environ:
                del os.environ[ACCOUNT_ENV_VAR]
        else:
            os.environ[ACCOUNT_ENV_VAR] = previous


class _WebState:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.lock = threading.Lock()


def _build_state(config_path: Path, selected_account: str | None = None) -> dict[str, Any]:
    config = load_raw_config(config_path)
    accounts = list_accounts(config_path)
    active = get_active_account(config_path) or ""
    account_id = selected_account or active or (next(iter(accounts.keys()), ""))
    groups = list_groups_for_account(config_path, account_id) if account_id else []

    account_items: list[dict[str, Any]] = []
    for aid, override in accounts.items():
        account_items.append({
            "id": aid,
            "enabled": bool(override.get("enabled", True)),
            "is_active": aid == active,
        })

    group_items: list[dict[str, Any]] = []
    for g in groups:
        gid = str(g.get("id", "")).strip()
        group_items.append({
            "id": gid,
            "name": str(g.get("name", "")),
            "url": str(g.get("url", "")),
            "included": bool(g.get("active", True)),
        })

    return {
        "active_account": active,
        "selected_account": account_id,
        "accounts": account_items,
        "groups": group_items,
        "posting": config.get("posting", {}),
    }


def _render_page() -> str:
    return r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>FBPost — Control Panel</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;600;700;800;900&family=Plus+Jakarta+Sans:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg:        #FFFDF5;
      --fg:        #1E293B;
      --muted:     #F1F5F9;
      --muted-fg:  #64748B;
      --card:      #FFFFFF;
      --border:    #E2E8F0;
      --accent:    #8B5CF6;
      --pink:      #F472B6;
      --yellow:    #FBBF24;
      --green:     #34D399;
      --red:       #F87171;

      --pop:   4px 4px 0px 0px var(--fg);
      --lift:  6px 6px 0px 0px var(--fg);
      --press: 2px 2px 0px 0px var(--fg);

      --r-sm:   8px;
      --r-md:   16px;
      --r-lg:   24px;
      --r-pill: 9999px;

      --heading: 'Outfit', system-ui, sans-serif;
      --body:    'Plus Jakarta Sans', system-ui, sans-serif;
      --bounce:  cubic-bezier(0.34, 1.56, 0.64, 1);
    }

    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: var(--body);
      font-size: 14px;
      color: var(--fg);
      background-color: var(--bg);
      background-image: radial-gradient(circle, #CBD5E1 1.2px, transparent 1.2px);
      background-size: 22px 22px;
      min-height: 100vh;
    }

    /* ── Floating deco shapes ───────────── */
    .deco { position: fixed; inset: 0; pointer-events: none; z-index: 0; overflow: hidden; }
    .deco-shape { position: absolute; opacity: .10; }
    .ds1 { width:320px;height:320px; background:var(--yellow); border-radius:50%; top:-90px; left:-70px; }
    .ds2 { width:190px;height:190px; background:var(--pink);   top:50px; right:160px; transform:rotate(18deg); }
    .ds3 { width:140px;height:140px; background:var(--accent); border-radius:50%; bottom:12%; left:6%; }
    .ds4 { width:240px;height:240px; background:var(--green);  border-radius:40% 60% 60% 40%; bottom:-60px; right:-50px; }

    /* ── Page wrapper ───────────────────── */
    .page {
      position: relative; z-index: 1;
      width: min(1380px, 97vw);
      margin: 0 auto;
      padding: 26px 0 60px;
      display: flex;
      flex-direction: column;
      gap: 20px;
    }

    /* ── Header ─────────────────────────── */
    .header {
      background: var(--card);
      border: 2px solid var(--fg);
      border-radius: var(--r-lg);
      padding: 14px 24px;
      box-shadow: 8px 8px 0 var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      flex-wrap: wrap;
    }

    .logo-row { display: flex; align-items: center; gap: 13px; }
    .logo-icon {
      width: 46px; height: 46px;
      background: var(--accent);
      border: 2px solid var(--fg);
      border-radius: var(--r-md);
      box-shadow: var(--pop);
      display: flex; align-items: center; justify-content: center;
      font-size: 22px; flex-shrink: 0;
    }
    .logo-name { font-family: var(--heading); font-weight: 900; font-size: 22px; letter-spacing: -.4px; }
    .logo-name em { color: var(--accent); font-style: normal; }
    .logo-sub { font-size: 12px; color: var(--muted-fg); font-weight: 500; margin-top: 1px; }

    .live-badge {
      display: inline-flex; align-items: center; gap: 7px;
      background: var(--card);
      border: 2px solid var(--fg);
      border-radius: var(--r-pill);
      padding: 5px 14px;
      font-family: var(--heading);
      font-weight: 700; font-size: 12px;
      box-shadow: var(--pop);
    }
    .live-dot {
      width: 8px; height: 8px; border-radius: 50%;
      background: var(--green);
      animation: blink 2s ease-in-out infinite;
    }
    @keyframes blink { 0%,100%{opacity:1} 50%{opacity:.25} }

    /* ── Grid ───────────────────────────── */
    .main-grid {
      display: grid;
      grid-template-columns: 310px 1fr;
      gap: 20px;
      align-items: start;
    }

    /* ── Card ───────────────────────────── */
    .card {
      background: var(--card);
      border: 2px solid var(--fg);
      border-radius: var(--r-lg);
      padding: 22px;
      box-shadow: 8px 8px 0 var(--border);
    }

    .card-title {
      font-family: var(--heading);
      font-weight: 800;
      font-size: 17px;
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 16px;
    }

    .t-icon {
      width: 30px; height: 30px;
      border: 2px solid var(--fg);
      border-radius: var(--r-sm);
      display: flex; align-items: center; justify-content: center;
      font-size: 15px; flex-shrink: 0;
      box-shadow: 3px 3px 0 var(--fg);
    }
    .ti-violet { background: var(--accent); }
    .ti-pink   { background: var(--pink);   }
    .ti-yellow { background: var(--yellow); }
    .ti-green  { background: var(--green);  }

    /* ── Stat boxes ─────────────────────── */
    .stats {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 10px;
      margin-bottom: 16px;
    }
    .stat {
      background: var(--muted);
      border: 2px solid var(--fg);
      border-radius: var(--r-md);
      padding: 11px 8px;
      text-align: center;
      box-shadow: 4px 4px 0 var(--fg);
      transition: transform .22s var(--bounce), box-shadow .22s var(--bounce);
      cursor: default;
    }
    .stat:hover { transform: translate(-2px,-2px); box-shadow: var(--lift); }
    .stat-n {
      font-family: var(--heading); font-weight: 900; font-size: 28px;
      line-height: 1; color: var(--accent);
    }
    .stat-l { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing:.05em; color: var(--muted-fg); margin-top: 2px; }

    /* ── Active badge ───────────────────── */
    .active-badge {
      display: inline-flex; align-items: center; gap: 5px;
      background: var(--yellow);
      border: 2px solid var(--fg);
      border-radius: var(--r-pill);
      padding: 4px 12px;
      font-family: var(--heading); font-weight: 800; font-size: 12px;
      box-shadow: 3px 3px 0 var(--fg);
    }

    /* ── Form row ───────────────────────── */
    .frow { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; align-items: center; }

    input[type=text], select {
      height: 40px; padding: 0 13px;
      border: 2px solid var(--fg); border-radius: var(--r-md);
      background: var(--card); color: var(--fg);
      font-family: var(--body); font-size: 13px;
      outline: none;
      box-shadow: 4px 4px 0 transparent;
      transition: box-shadow .18s var(--bounce), border-color .15s;
    }
    input[type=text]:focus, select:focus {
      border-color: var(--accent);
      box-shadow: 4px 4px 0 var(--accent);
    }
    input[type=text] { font-family: 'Courier New', monospace; font-size: 12px; min-width: 180px; }
    select { cursor: pointer; }

    /* ── Buttons ────────────────────────── */
    button {
      display: inline-flex; align-items: center; gap: 5px;
      cursor: pointer;
      font-family: var(--heading); font-weight: 700; font-size: 13px;
      padding: 8px 16px;
      border: 2px solid var(--fg); border-radius: var(--r-pill);
      background: var(--card); color: var(--fg);
      box-shadow: var(--pop);
      transition: transform .22s var(--bounce), box-shadow .22s var(--bounce), background .15s;
      white-space: nowrap;
    }
    button:hover:not(:disabled) { transform: translate(-2px,-2px); box-shadow: var(--lift); }
    button:active:not(:disabled) { transform: translate(2px,2px); box-shadow: var(--press); }
    button:disabled { opacity: .42; cursor: not-allowed; transform: none !important; }

    .btn-primary { background: var(--accent); color: #fff; }
    .btn-primary:hover:not(:disabled) { background: #7c3aed; }
    .btn-green   { background: var(--green); }
    .btn-green:hover:not(:disabled) { background: #10b981; }
    .btn-yellow  { background: var(--yellow); }
    .btn-red     { background: var(--red); color: #fff; }
    .btn-red:hover:not(:disabled) { background: #ef4444; }

    .sm-btn {
      padding: 5px 11px; font-size: 12px; border-radius: var(--r-pill);
      box-shadow: 3px 3px 0 var(--fg);
    }
    .sm-btn:hover:not(:disabled) { transform: translate(-1px,-1px); box-shadow: 4px 4px 0 var(--fg); }
    .sm-btn:active:not(:disabled) { transform: translate(1px,1px); box-shadow: 1px 1px 0 var(--fg); }

    /* ── Table ──────────────────────────── */
    .tbl-wrap {
      border: 2px solid var(--fg); border-radius: var(--r-md);
      box-shadow: 4px 4px 0 var(--fg);
      overflow-x: auto;
    }
    table { width: 100%; border-collapse: collapse; }
    thead { background: var(--muted); }
    th {
      padding: 9px 13px; text-align: left;
      font-family: var(--heading); font-size: 11px; font-weight: 800;
      text-transform: uppercase; letter-spacing: .07em; color: var(--muted-fg);
      border-bottom: 2px solid var(--fg); white-space: nowrap;
    }
    td { padding: 9px 13px; border-bottom: 1px solid var(--border); font-size: 13px; vertical-align: middle; }
    tbody tr:last-child td { border-bottom: none; }
    tbody tr:hover td { background: #FFFBEC; }
    .mono { font-family: 'Courier New', monospace; font-size: 12px; color: var(--muted-fg); }

    /* ── Pills ──────────────────────────── */
    .pill {
      display: inline-flex; align-items: center; gap: 4px;
      padding: 3px 9px;
      border: 2px solid var(--fg); border-radius: var(--r-pill);
      font-family: var(--heading); font-weight: 700; font-size: 11px;
      box-shadow: 2px 2px 0 var(--fg);
      white-space: nowrap;
    }
    .p-green  { background: var(--green); }
    .p-orange { background: #FED7AA; }
    .p-violet { background: #EDE9FE; color: #5b21b6; }
    .p-yellow { background: var(--yellow); }
    .p-gray   { background: var(--muted); color: var(--muted-fg); }

    /* ── Toolbar ────────────────────────── */
    .toolbar {
      background: var(--muted);
      border: 2px solid var(--fg); border-radius: var(--r-md);
      padding: 14px 16px; margin-bottom: 20px;
      box-shadow: 4px 4px 0 var(--fg);
    }
    .toolbar-lbl {
      font-family: var(--heading); font-size: 11px; font-weight: 800;
      text-transform: uppercase; letter-spacing: .07em; color: var(--muted-fg);
      margin-bottom: 10px;
    }
    .toolbar-btns { display: flex; flex-wrap: wrap; gap: 8px; }

    /* ── Divider ────────────────────────── */
    .divider {
      display: flex; align-items: center; gap: 10px;
      margin: 20px 0 14px;
    }
    .divider-label {
      font-family: var(--heading); font-weight: 800; font-size: 15px;
      display: flex; align-items: center; gap: 8px; white-space: nowrap;
    }
    .divider::before, .divider::after {
      content: ''; flex: 1; height: 2px;
      background: repeating-linear-gradient(90deg, var(--fg) 0 5px, transparent 5px 11px);
    }

    /* ── Empty state ────────────────────── */
    .empty { text-align: center; padding: 34px 16px; color: var(--muted-fg); }
    .empty-ico { font-size: 38px; display: block; margin-bottom: 8px; }
    .empty-txt { font-family: var(--heading); font-weight: 700; font-size: 14px; }

    /* ── Toast ──────────────────────────── */
    #toast {
      position: fixed; bottom: 20px; right: 20px; z-index: 9999;
      max-width: min(380px, calc(100vw - 32px));
      background: var(--card);
      border: 2px solid var(--fg); border-radius: var(--r-lg);
      padding: 12px 16px;
      font-family: var(--heading); font-weight: 700; font-size: 14px;
      display: flex; align-items: center; gap: 10px;
      box-shadow: var(--pop);
      transform: translateY(20px) scale(.95);
      opacity: 0; pointer-events: none;
      transition: opacity .22s var(--bounce), transform .22s var(--bounce);
    }
    #toast.show { transform: translateY(0) scale(1); opacity: 1; }
    #toast.ok  { border-left: 6px solid var(--green); }
    #toast.err { border-left: 6px solid var(--red); }

    @media (prefers-reduced-motion: reduce) {
      *, button { transition: none !important; animation: none !important; }
    }
    @media (max-width: 860px) {
      .main-grid { grid-template-columns: 1fr; }
      th:nth-child(3), td:nth-child(3) { display: none; }
    }
  </style>
</head>
<body>

<div class="deco" aria-hidden="true">
  <div class="deco-shape ds1"></div>
  <div class="deco-shape ds2"></div>
  <div class="deco-shape ds3"></div>
  <div class="deco-shape ds4"></div>
</div>

<div class="page">

  <!-- Header -->
  <header class="header">
    <div class="logo-row">
      <div class="logo-icon">📘</div>
      <div>
        <div class="logo-name"><em>FB</em>Post</div>
        <div class="logo-sub">Control Panel · Realtime API</div>
      </div>
    </div>
    <div class="live-badge">
      <span class="live-dot"></span>
      <span id="lastUpdated">Connecting…</span>
    </div>
  </header>

  <div class="main-grid">

    <!-- Accounts -->
    <aside>
      <div class="card">
        <div class="card-title">
          <div class="t-icon ti-violet">👤</div>
          Accounts
        </div>

        <div class="stats">
          <div class="stat"><div class="stat-n" id="statTotal">0</div><div class="stat-l">Total</div></div>
          <div class="stat"><div class="stat-n" style="color:var(--green)" id="statEnabled">0</div><div class="stat-l">On</div></div>
          <div class="stat"><div class="stat-n" style="color:var(--pink)"  id="statDisabled">0</div><div class="stat-l">Off</div></div>
        </div>

        <div class="frow" style="margin-bottom:14px">
          <span class="active-badge">⚡ <span id="activeName">—</span></span>
        </div>

        <div class="frow">
          <select id="accountSelect" style="flex:1;min-width:0"></select>
        </div>

        <div class="frow">
          <input id="newAccountId" type="text" placeholder="new-account-id" style="flex:1;min-width:0">
          <button id="addAccountBtn" class="btn-primary" type="button">＋ Add</button>
        </div>

        <div class="tbl-wrap">
          <table>
            <thead><tr><th>Account</th><th>State</th><th>Actions</th></tr></thead>
            <tbody id="accountsBody">
              <tr><td colspan="3"><div class="empty"><span class="empty-ico">⏳</span><span class="empty-txt">Loading…</span></div></td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </aside>

    <!-- Actions + Groups -->
    <section>
      <div class="card">
        <div class="card-title">
          <div class="t-icon ti-pink">⚙️</div>
          <span id="selectedTitle">Select an account</span>
        </div>

        <div class="toolbar">
          <div class="toolbar-lbl">Quick Actions</div>
          <div class="toolbar-btns" id="selectedActions">
            <button type="button" data-action="test_session">🔍 Test Session</button>
            <button type="button" data-action="setup_session">🔐 Setup Session</button>
            <button type="button" data-action="scrape_groups" class="btn-yellow">🕷️ Scrape Groups</button>
            <button type="button" data-action="run_once_dry">🧪 Run Dry</button>
            <button type="button" data-action="run_once_live" class="btn-green">🚀 Run Live</button>
          </div>
        </div>

        <div class="divider">
          <div class="divider-label">
            <div class="t-icon ti-violet" style="width:24px;height:24px;font-size:13px;box-shadow:2px 2px 0 var(--fg)">👥</div>
            Groups
          </div>
          <span class="pill p-violet" id="groupCount" style="margin-left:auto">0 groups</span>
        </div>

        <div class="frow">
          <input id="groupFilter" type="text" placeholder="filter by id or name…" style="min-width:170px;flex:1;max-width:260px">
          <select id="groupStatusFilter">
            <option value="all">All statuses</option>
            <option value="included">Included only</option>
            <option value="excluded">Excluded only</option>
          </select>
        </div>

        <div class="tbl-wrap">
          <table>
            <thead><tr><th>ID</th><th>Name</th><th>URL</th><th>Status</th><th>Toggle</th></tr></thead>
            <tbody id="groupsBody">
              <tr><td colspan="5"><div class="empty"><span class="empty-ico">👥</span><span class="empty-txt">No groups loaded yet</span></div></td></tr>
            </tbody>
          </table>
        </div>

      </div>
    </section>
  </div>
</div>

<div id="toast" role="alert" aria-live="polite"></div>

<script>
  let selectedAccount = '';
  let isBusy = false;
  let groupsSnapshot = [];
  let toastTimer = null;

  function esc(v) {
    const d = document.createElement('div');
    d.innerText = String(v ?? '');
    return d.innerHTML;
  }

  function toast(msg, isErr = false) {
    const el = document.getElementById('toast');
    el.innerHTML = (isErr ? '❌ ' : '✅ ') + esc(msg);
    el.className = 'show ' + (isErr ? 'err' : 'ok');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.className = ''; }, 3200);
  }

  function setUpdated() {
    document.getElementById('lastUpdated').textContent = new Date().toLocaleTimeString();
  }

  function setBusy(b) {
    isBusy = b;
    document.querySelectorAll('button').forEach(btn => btn.disabled = !!b);
  }

  async function callAction(action, accountId, groupId = '') {
    if (isBusy) return;
    setBusy(true);
    try {
      const res = await fetch('/api/action', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, account_id: accountId, group_id: groupId })
      });
      const data = await res.json();
      data.ok ? toast(data.message || 'Done!') : toast(data.error || 'Failed.', true);
    } catch (e) {
      toast(String(e), true);
    } finally {
      setBusy(false);
      await loadState();
    }
  }

  function renderAccounts(data) {
    const rows = data.accounts || [];
    const on = rows.filter(x => x.enabled).length;
    document.getElementById('statTotal').textContent    = rows.length;
    document.getElementById('statEnabled').textContent  = on;
    document.getElementById('statDisabled').textContent = Math.max(0, rows.length - on);
    document.getElementById('activeName').textContent   = data.active_account || '—';

    const sel = document.getElementById('accountSelect');
    sel.innerHTML = rows.map(a =>
      `<option value="${esc(a.id)}">${esc(a.id)}${a.enabled ? '' : ' (off)'}</option>`
    ).join('');
    if (selectedAccount) sel.value = selectedAccount;

    const tbody = document.getElementById('accountsBody');
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="3"><div class="empty"><span class="empty-ico">🫙</span><span class="empty-txt">No accounts yet</span></div></td></tr>';
      return;
    }
    tbody.innerHTML = rows.map(a => {
      const sp  = a.enabled
        ? `<span class="pill p-green">● On</span>`
        : `<span class="pill p-orange">● Off</span>`;
      const act = a.is_active ? `<span class="pill p-yellow" style="margin-left:3px">⚡</span>` : '';
      const btns = [
        `<button class="sm-btn btn-yellow" type="button" onclick="callAction('set_active','${esc(a.id)}')">⚡</button>`,
        a.enabled
          ? `<button class="sm-btn" type="button" onclick="callAction('disable_account','${esc(a.id)}')">⏸</button>`
          : `<button class="sm-btn btn-green" type="button" onclick="callAction('enable_account','${esc(a.id)}')">▶</button>`,
      ];
      if (!a.is_active) btns.push(`<button class="sm-btn btn-red" type="button" onclick="callAction('delete_account','${esc(a.id)}')">🗑</button>`);
      return `<tr>
        <td>
          <a href="#" class="mono" style="color:var(--accent);font-weight:700;text-decoration:none"
             onclick="selectAccount('${esc(a.id)}');return false;">${esc(a.id)}</a>${act}
        </td>
        <td>${sp}</td>
        <td><div style="display:flex;gap:5px;flex-wrap:wrap">${btns.join('')}</div></td>
      </tr>`;
    }).join('');
  }

  function renderGroups(data) {
    const raw = data.groups || [];
    const txt = (document.getElementById('groupFilter').value || '').trim().toLowerCase();
    const sf  = document.getElementById('groupStatusFilter').value || 'all';
    const rows = raw.filter(g => {
      const m  = !txt || String(g.id).toLowerCase().includes(txt) || String(g.name).toLowerCase().includes(txt);
      const inc = !!g.included;
      return m && (sf === 'all' || (sf === 'included' && inc) || (sf === 'excluded' && !inc));
    });

    document.getElementById('groupCount').textContent = `${rows.length} / ${raw.length}`;

    const tbody = document.getElementById('groupsBody');
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="5"><div class="empty"><span class="empty-ico">🫙</span><span class="empty-txt">No groups match filter</span></div></td></tr>';
      return;
    }
    tbody.innerHTML = rows.map(g => {
      const inc = !!g.included;
      return `<tr>
        <td class="mono">${esc(g.id)}</td>
        <td style="font-weight:600">${esc(g.name)}</td>
        <td><a href="${esc(g.url)}" target="_blank" style="color:var(--accent);font-size:12px;font-weight:700">↗ Open</a></td>
        <td><span class="pill ${inc ? 'p-green' : 'p-orange'}">${inc ? '✓ In' : '✕ Out'}</span></td>
        <td>
          <button class="sm-btn ${inc ? 'btn-red' : 'btn-green'}" type="button"
            onclick="callAction('${inc ? 'exclude' : 'include'}_group','${esc(selectedAccount)}','${esc(g.id)}')">
            ${inc ? 'Exclude' : 'Include'}
          </button>
        </td>
      </tr>`;
    }).join('');
  }

  async function loadState() {
    const q = selectedAccount ? `?account=${encodeURIComponent(selectedAccount)}` : '';
    try {
      const res = await fetch('/api/state' + q, { cache: 'no-store' });
      const data = await res.json();
      selectedAccount = data.selected_account || data.active_account || '';
      groupsSnapshot  = data.groups || [];
      document.getElementById('selectedTitle').textContent =
        selectedAccount ? `Actions — ${selectedAccount}` : 'Select an account';
      renderAccounts(data);
      renderGroups({ groups: groupsSnapshot });
      setUpdated();
    } catch {
      document.getElementById('lastUpdated').textContent = 'Error';
    }
  }

  function selectAccount(id) { selectedAccount = id; loadState(); }

  document.getElementById('addAccountBtn').addEventListener('click', async () => {
    const inp = document.getElementById('newAccountId');
    const id  = (inp.value || '').trim();
    if (!id) { toast('Account id is required.', true); return; }
    await callAction('add_account', id);
    inp.value = '';
  });

  document.getElementById('selectedActions').addEventListener('click', async ev => {
    const btn = ev.target.closest('button[data-action]');
    if (!btn) return;
    if (!selectedAccount) { toast('Please select an account first.', true); return; }
    await callAction(btn.dataset.action, selectedAccount);
  });

  document.getElementById('groupFilter').addEventListener('input',        () => renderGroups({ groups: groupsSnapshot }));
  document.getElementById('groupStatusFilter').addEventListener('change', () => renderGroups({ groups: groupsSnapshot }));
  document.getElementById('accountSelect').addEventListener('change', ev => {
    const v = (ev.target.value || '').trim();
    if (v) selectAccount(v);
  });

  loadState();
  setInterval(loadState, 3000);
</script>
</body>
</html>
"""


def _execute_account_action(config_path: Path, action: str, account_id: str, group_id: str = "") -> tuple[bool, str]:
    if action == "add_account":
        return add_account(config_path, account_id)
    if action == "delete_account":
        return delete_account(config_path, account_id)
    if action == "enable_account":
        return set_account_enabled(config_path, account_id, True)
    if action == "disable_account":
        return set_account_enabled(config_path, account_id, False)
    if action == "set_active":
        return set_active_account(config_path, account_id)
    if action == "exclude_group":
        return set_group_included(config_path, account_id, group_id, False)
    if action == "include_group":
        return set_group_included(config_path, account_id, group_id, True)

    with _account_env(account_id):
        if action == "test_session":
            ok = validate_session(config_path)
            return (ok, "Session valid ✓" if ok else "Session invalid ✗")
        if action == "setup_session":
            ensure_session(config_path, force_relogin=False)
            return True, "Setup session flow finished."
        if action == "scrape_groups":
            scrape_groups(config_path, force=True)
            return True, "Group scrape completed."
        if action == "run_once_dry":
            run_scheduler(config_path, run_once=True, force_dry_run=True)
            return True, "Dry run completed."
        if action == "run_once_live":
            run_scheduler(config_path, run_once=True, force_dry_run=False)
            return True, "Live run completed."

    return False, f"Unknown action '{action}'."


def run_web_ui(*, config_path: Path, host: str, port: int) -> None:
    state = _WebState(config_path)

    class Handler(BaseHTTPRequestHandler):
        def _send_html(self, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            if parsed.path == "/api/state":
                selected_account = (qs.get("account") or [""])[0].strip() or None
                payload = _build_state(state.config_path, selected_account)
                self._send_json({"ok": True, "data": payload, **payload})
                return
            if parsed.path == "/":
                self._send_html(_render_page())
                return
            self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/api/action":
                self.send_error(404)
                return
            content_len = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(content_len)
            try:
                payload    = json.loads(raw.decode("utf-8", errors="ignore") or "{}")
                action     = str(payload.get("action", "")).strip()
                account_id = str(payload.get("account_id", "")).strip()
                group_id   = str(payload.get("group_id", "")).strip()

                if not account_id:
                    self._send_json({"ok": False, "error": "Account id is required."}, status=400)
                    return

                with state.lock:
                    ok, result = _execute_account_action(state.config_path, action, account_id, group_id)

                state_payload = _build_state(state.config_path, account_id or None)
                if ok:
                    self._send_json({"ok": True, "message": result, "state": state_payload})
                else:
                    self._send_json({"ok": False, "error": result, "state": state_payload}, status=400)
            except Exception as exc:
                self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=500)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

    server = ThreadingHTTPServer((host, int(port)), Handler)
    print(f"UI running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    finally:
        server.server_close()