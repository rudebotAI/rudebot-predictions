"""
Live Dashboard v2 -- in-process HTTP server.
Serves a self-refreshing HTML page and a /state.json endpoint.
Binds to $PORT (Railway) or 8080 locally.
Runs in a background thread so the main scan loop keeps going.
"""
import json
import os
import threading
import logging
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Updated by main.py via set_state_provider(...)
_state_provider: Optional[Callable[[], dict]] = None


def set_state_provider(fn: Callable[[], dict]):
    global _state_provider
    _state_provider = fn


def _empty_state() -> dict:
    return {
        "mode": "paper",
        "bankroll": 0,
        "scan_number": 0,
        "last_scan_at": None,
        "kalshi_markets": 0,
        "poly_markets": 0,
        "ev_opportunities": 0,
        "arb_opportunities": 0,
        "risk_status": "Active",
        "performance": {
            "total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
            "total_pnl": 0, "avg_pnl": 0, "best_trade": 0, "worst_trade": 0,
            "open_positions": 0, "skipped": 0,
        },
        "open_positions": [],
        "recent_closed": [],
        "recent_signals": [],
        "errors": [],
        "go_live_ready": False,
    }


def _render_html(s: dict) -> str:
    perf = s.get("performance") or {}
    total_pnl = float(perf.get("total_pnl", 0) or 0)
    avg_pnl = float(perf.get("avg_pnl", 0) or 0)
    win_rate = float(perf.get("win_rate", 0) or 0)
    total_trades = int(perf.get("total_trades", 0) or 0)
    wins = int(perf.get("wins", 0) or 0)
    losses = int(perf.get("losses", 0) or 0)
    open_count = int(perf.get("open_positions", 0) or 0)
    best = float(perf.get("best_trade", 0) or 0)
    worst = float(perf.get("worst_trade", 0) or 0)

    mode = (s.get("mode") or "paper").upper()
    scan_num = s.get("scan_number", 0)
    last_scan = s.get("last_scan_at") or "-"
    k_n = s.get("kalshi_markets", 0)
    p_n = s.get("poly_markets", 0)
    ev_n = s.get("ev_opportunities", 0)
    risk_status = s.get("risk_status", "Active")

    pnl_color = "#4ade80" if total_pnl >= 0 else "#f87171"
    status_class = "status-active" if risk_status == "Active" else "status-halted"

    # Go-live bar: 100 closed trades + positive total P&L
    go_live_progress = min(100, int(total_trades))
    go_live_pnl_ok = total_pnl > 0
    go_live_ready = total_trades >= 100 and go_live_pnl_ok

    def _row_pos(p: dict) -> str:
        sig = p.get("signal", "?")
        tag_cls = "tag-yes" if sig == "YES" else "tag-no"
        q = (p.get("question") or p.get("market_id") or "?")[:70]
        entry = float(p.get("entry_price", 0) or 0)
        size = float(p.get("size_usd", 0) or 0)
        platform = p.get("platform", "?")
        return (
            f'<div class="card"><span class="tag {tag_cls}">{sig}</span>'
            f'<strong>{q}</strong><br>'
            f'<small>{platform} &middot; Entry {entry:.3f} &middot; ${size:.2f}</small></div>'
        )

    def _row_closed(t: dict) -> str:
        pnl = float(t.get("pnl", 0) or 0)
        pnl_pct = float(t.get("pnl_pct", 0) or 0)
        cls = "pnl-pos" if pnl >= 0 else "pnl-neg"
        q = (t.get("question") or t.get("market_id") or "?")[:70]
        reason = t.get("close_reason", "")
        return (
            f'<div class="card"><span class="{cls}">{"+" if pnl>=0 else ""}${pnl:.2f} '
            f'({"+" if pnl_pct>=0 else ""}{pnl_pct:.1f}%)</span> '
            f'<strong>{q}</strong> <small>&middot; {reason}</small></div>'
        )

    def _row_sig(s_: dict) -> str:
        sig = s_.get("signal", "?")
        q = (s_.get("question") or "?")[:70]
        ev = float(s_.get("ev", 0) or 0)
        edge = float(s_.get("edge", 0) or 0)
        size = float(s_.get("size_usd", 0) or 0)
        return (
            f'<div class="card"><span class="tag">{sig}</span>'
            f'<strong>{q}</strong> <span class="ev">EV {ev:.3f}</span>'
            f' <small>&middot; Edge {edge:.3f} &middot; ${size:.2f}</small></div>'
        )

    open_positions = s.get("open_positions") or []
    recent_closed = s.get("recent_closed") or []
    recent_signals = s.get("recent_signals") or []
    errors = s.get("errors") or []

    positions_html = "".join(_row_pos(p) for p in open_positions[:50]) or '<div class="empty">No open positions</div>'
    closed_html = "".join(_row_closed(t) for t in recent_closed[:25]) or '<div class="empty">No closed trades yet</div>'
    signals_html = "".join(_row_sig(x) for x in recent_signals[-15:][::-1]) or '<div class="empty">No signals yet</div>'
    errors_html = "".join(f'<div class="card error-card">{e}</div>' for e in errors[-5:])

    go_live_bar_color = "#4ade80" if go_live_ready else ("#fbbf24" if go_live_pnl_ok else "#f87171")
    go_live_text = (
        "READY" if go_live_ready
        else f"{total_trades}/100 closed trades" + (" (P&L positive)" if go_live_pnl_ok else " (P&L negative)")
    )

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="10">
<title>PredBot Dashboard</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0f172a;color:#e2e8f0;font-family:-apple-system,system-ui,sans-serif;padding:20px;max-width:1400px;margin:0 auto}}
.header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;flex-wrap:wrap;gap:12px}}
.header h1{{font-size:22px;color:#f8fafc}}
.mode-badge{{background:#1e3a5f;color:#7dd3fc;padding:4px 12px;border-radius:12px;font-size:12px;margin-left:10px}}
.status{{padding:6px 14px;border-radius:20px;font-size:12px;font-weight:600}}
.status-active{{background:#065f46;color:#6ee7b7}}
.status-halted{{background:#7f1d1d;color:#fca5a5}}
.golive{{background:#1e293b;border-radius:12px;padding:14px 16px;margin-bottom:16px;display:flex;align-items:center;gap:14px;flex-wrap:wrap}}
.golive .label{{font-size:13px;color:#94a3b8}}
.golive .bar{{flex:1;height:10px;background:#0f172a;border-radius:6px;overflow:hidden;min-width:160px}}
.golive .fill{{height:100%;transition:width .4s}}
.golive .status-text{{font-weight:700;font-size:13px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:16px}}
.stat{{background:#1e293b;border-radius:10px;padding:14px;text-align:center}}
.stat .num{{font-size:22px;font-weight:700;color:#f8fafc}}
.stat .label{{font-size:11px;color:#94a3b8;margin-top:3px;text-transform:uppercase;letter-spacing:.5px}}
.cols{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
@media (max-width:880px){{.cols{{grid-template-columns:1fr}}}}
.section{{background:#1e293b;border-radius:10px;padding:14px;margin-bottom:14px}}
.section h2{{font-size:14px;color:#94a3b8;margin-bottom:10px;border-bottom:1px solid #334155;padding-bottom:6px;text-transform:uppercase;letter-spacing:.5px}}
.card{{background:#0f172a;border-radius:8px;padding:9px 12px;margin-bottom:6px;font-size:13px;line-height:1.45}}
.tag{{display:inline-block;padding:2px 7px;border-radius:5px;font-size:10px;font-weight:700;background:#334155;margin-right:5px;letter-spacing:.3px}}
.tag-yes{{background:#065f46;color:#6ee7b7}}
.tag-no{{background:#7f1d1d;color:#fca5a5}}
.ev{{color:#fbbf24;font-weight:600}}
.empty{{color:#64748b;font-style:italic;padding:6px 0}}
.error-card{{border-left:3px solid #f87171;color:#fca5a5}}
.pnl-pos{{color:#4ade80;font-weight:700}}
.pnl-neg{{color:#f87171;font-weight:700}}
.footer{{text-align:center;color:#475569;font-size:11px;margin-top:20px}}
a{{color:#7dd3fc}}
</style></head>
<body>
<div class="header">
  <div><h1>PredBot Dashboard <span class="mode-badge">{mode} MODE</span></h1></div>
  <span class="status {status_class}">{risk_status}</span>
</div>

<div class="golive">
  <div class="label">Go-Live Bar (100 closed trades + positive P&amp;L):</div>
  <div class="bar"><div class="fill" style="width:{go_live_progress}%;background:{go_live_bar_color}"></div></div>
  <div class="status-text" style="color:{go_live_bar_color}">{go_live_text}</div>
</div>

<div class="grid">
  <div class="stat"><div class="num" style="color:{pnl_color}">${total_pnl:.2f}</div><div class="label">Total P&amp;L</div></div>
  <div class="stat"><div class="num">{total_trades}</div><div class="label">Closed Trades</div></div>
  <div class="stat"><div class="num">{win_rate:.0f}%</div><div class="label">Win Rate ({wins}W/{losses}L)</div></div>
  <div class="stat"><div class="num">{open_count}</div><div class="label">Open</div></div>
  <div class="stat"><div class="num">${avg_pnl:.2f}</div><div class="label">Avg Trade</div></div>
  <div class="stat"><div class="num" style="color:#4ade80">${best:.2f}</div><div class="label">Best</div></div>
  <div class="stat"><div class="num" style="color:#f87171">${worst:.2f}</div><div class="label">Worst</div></div>
  <div class="stat"><div class="num">{scan_num}</div><div class="label">Scans</div></div>
  <div class="stat"><div class="num">{k_n + p_n}</div><div class="label">Markets ({k_n}k/{p_n}p)</div></div>
  <div class="stat"><div class="num">{ev_n}</div><div class="label">EV Signals (cycle)</div></div>
</div>

<div class="cols">
  <div class="section"><h2>Open Positions</h2>{positions_html}</div>
  <div class="section"><h2>Recent Closed</h2>{closed_html}</div>
</div>
<div class="section"><h2>Recent Signals</h2>{signals_html}</div>
{'<div class="section"><h2>Errors</h2>' + errors_html + '</div>' if errors_html else ''}
<div class="footer">Last scan: {last_scan} &middot; Auto-refresh 10s &middot; <a href="/state.json">state.json</a></div>
</body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # silence default access logs
        return

    def do_GET(self):
        try:
            state = _state_provider() if _state_provider else _empty_state()
        except Exception as e:
            logger.warning(f"dashboard: state provider raised {type(e).__name__}: {e}")
            state = _empty_state()

        if self.path.startswith("/state.json"):
            body = json.dumps(state, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path.startswith("/healthz"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
            return

        body = _render_html(state).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start(port: Optional[int] = None) -> ThreadingHTTPServer:
    """Start the dashboard HTTP server in a daemon thread. Returns the server."""
    port = port or int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="dashboard-http")
    thread.start()
    logger.info(f"Dashboard HTTP server listening on 0.0.0.0:{port}")
    return server


# Back-compat shim so anything still importing update_dashboard doesn't crash.
def update_dashboard(state: dict):  # pragma: no cover
    """Deprecated: dashboard is now served live via HTTP. No-op."""
    return
