"""
Live Dashboard -- Generates a self-refreshing HTML file the user can open in a browser.
Updated after every scan cycle by main.py.
"""

import json
import time
from pathlib import Path
from datetime import datetime

DASHBOARD_PATH = Path(__file__).parent / "dashboard.html"


def update_dashboard(state: dict):
    """Write current bot state to dashboard.html."""
    now = datetime.now().strftime("%I:%M:%S %p")
    mode = state.get("mode", "paper").upper()
    bankroll = state.get("bankroll", 0)
    scan_num = state.get("scan_number", 0)
    poly_count = state.get("poly_markets", 0)
    kalshi_count = state.get("kalshi_markets", 0)
    ev_count = state.get("ev_opportunities", 0)
    arb_count = state.get("arb_opportunities", 0)
    div_count = state.get("div_signals", 0)
    status = state.get("risk_status", "Active")
    daily_pnl = state.get("daily_pnl", 0)
    total_pnl = state.get("total_pnl", 0)
    total_trades = state.get("total_trades", 0)
    wins = state.get("wins", 0)
    losses = state.get("losses", 0)
    win_rate = state.get("win_rate", 0)
    open_positions = state.get("open_positions", [])
    recent_signals = state.get("recent_signals", [])
    errors = state.get("errors", [])
    scan_interval = state.get("scan_interval", 120)

    positions_html = ""
    for p in open_positions:
        positions_html += f"""
        <div class="card">
            <span class="tag {'tag-yes' if p.get('signal')=='YES' else 'tag-no'}">{p.get('signal','?')}</span>
            <strong>{p.get('question','?')[:60]}</strong><br>
            <small>{p.get('platform','?')} &middot; Entry: {p.get('entry_price',0):.3f} &middot; ${p.get('size_usd',0):.2f}</small>
        </div>"""
    if not open_positions:
        positions_html = '<div class="empty">No open positions</div>'

    signals_html = ""
    for s in recent_signals[-10:]:
        research_tag = ""
        if s.get("research_direction"):
            rd = s["research_direction"]
            rc = "#4ade80" if rd == "YES" else "#f87171" if rd == "NO" else "#94a3b8"
            research_tag = f' <span style="color:{rc};font-weight:600">Research: {rd}</span>'
        signals_html += f"""
        <div class="card">
            <span class="tag">{s.get('signal','?')}</span>
            <strong>{s.get('question','?')[:50]}</strong>
            <span class="ev">EV {s.get('ev',0):.3f}</span>
            <small>&middot; Edge {s.get('edge',0):.3f} &middot; ${s.get('size_usd',0):.2f}</small>
            {research_tag}
        </div>"""
    if not recent_signals:
        signals_html = '<div class="empty">No signals yet -- waiting for first scan</div>'

    errors_html = ""
    for e in errors[-5:]:
        errors_html += f'<div class="card error-card">{e}</div>'

    pnl_color = "#4ade80" if total_pnl >= 0 else "#f87171"
    daily_color = "#4ade80" if daily_pnl >= 0 else "#f87171"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="10">
<title>PredBot Dashboard</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ background:#0f172a; color:#e2e8f0; font-family:-apple-system,system-ui,sans-serif; padding:20px; }}
  .header {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:24px; }}
  .header h1 {{ font-size:24px; color:#f8fafc; }}
  .header .status {{ padding:6px 16px; border-radius:20px; font-size:13px; font-weight:600; }}
  .status-active {{ background:#065f46; color:#6ee7b7; }}
  .status-halted {{ background:#7f1d1d; color:#fca5a5; }}
  .mode-badge {{ background:#1e3a5f; color:#7dd3fc; padding:4px 12px; border-radius:12px; font-size:12px; margin-left:12px; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin-bottom:24px; }}
  .stat {{ background:#1e293b; border-radius:12px; padding:16px; text-align:center; }}
  .stat .num {{ font-size:28px; font-weight:700; color:#f8fafc; }}
  .stat .label {{ font-size:12px; color:#94a3b8; margin-top:4px; }}
  .section {{ background:#1e293b; border-radius:12px; padding:16px; margin-bottom:16px; }}
  .section h2 {{ font-size:16px; color:#94a3b8; margin-bottom:12px; border-bottom:1px solid #334155; padding-bottom:8px; }}
  .card {{ background:#0f172a; border-radius:8px; padding:10px 14px; margin-bottom:8px; font-size:13px; line-height:1.5; }}
  .tag {{ display:inline-block; padding:2px 8px; border-radius:6px; font-size:11px; font-weight:600; background:#334155; margin-right:6px; }}
  .tag-yes {{ background:#065f46; color:#6ee7b7; }}
  .tag-no {{ background:#7f1d1d; color:#fca5a5; }}
  .ev {{ color:#fbbf24; margin-left:8px; font-weight:600; }}
  .empty {{ color:#64748b; font-style:italic; padding:8px 0; }}
  .error-card {{ border-left:3px solid #f87171; color:#fca5a5; }}
  .footer {{ text-align:center; color:#475569; font-size:12px; margin-top:24px; }}
  .pnl {{ font-weight:700; }}
</style>
</head>
<body>
  <div class="header">
    <div>
      <h1>PredBot Dashboard</h1>
      <span class="mode-badge">{mode} MODE</span>
    </div>
    <div>
      <span class="status {'status-active' if status=='Active' else 'status-halted'}">{status}</span>
    </div>
  </div>

  <div class="grid">
    <div class="stat"><div class="num">{scan_num}</div><div class="label">Scans</div></div>
    <div class="stat"><div class="num">{poly_count + kalshi_count}</div><div class="label">Markets</div></div>
    <div class="stat"><div class="num">{ev_count}</div><div class="label">EV Signals</div></div>
    <div class="stat"><div class="num">{arb_count}</div><div class="label">Arb Signals</div></div>
    <div class="stat"><div class="num" style="color:{pnl_color}">${total_pnl:.2f}</div><div class="label">Total P&L</div></div>
    <div class="stat"><div class="num" style="color:{daily_color}">${daily_pnl:.2f}</div><div class="label">Daily P&L</div></div>
    <div class="stat"><div class="num">{total_trades}</div><div class="label">Trades (W:{wins} L:{losses})</div></div>
    <div class="stat"><div class="num">{win_rate:.0f}%</div><div class="label">Win Rate</div></div>
  </div>

  <div class="section">
    <h2>Open Positions</h2>
    {positions_html}
  </div>

  <div class="section">
    <h2>Recent Signals</h2>
    {signals_html}
  </div>

  {'<div class="section"><h2>Errors</h2>' + errors_html + '</div>' if errors_html else ''}

  <div class="footer">
    Last updated: {now} &middot; Bankroll: ${bankroll:.2f} &middot; Auto-refreshes every 10s
  </div>
</body>
</html>"""

    DASHBOARD_PATH.write_text(html)
