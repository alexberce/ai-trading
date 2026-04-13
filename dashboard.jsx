import { useState, useEffect, useCallback } from "react";

const MOCK_DATA = {
  updated_at: new Date().toISOString(),
  last_scan: new Date().toISOString(),
  portfolio: {
    bankroll: 2000, initial_bankroll: 2000, total_return: 0,
    total_pnl_closed: 0, peak_bankroll: 2000, drawdown: 0,
    open_positions: 0, total_exposure: 0, exposure_pct: 0,
    closed_trades: 0, wins: 0, losses: 0, win_rate: 0,
    avg_win: 0, avg_loss: 0, is_halted: false, halt_reason: "",
  },
  open_positions: [],
  closed_positions: [],
  opportunities: [
    { question: "Waiting for first scan...", market_price: 0, estimated_prob: 0, edge: 0, effective_edge: 0, direction: "—", confidence: 0, score: 0, sizing: {}, reasoning: "Run `python main.py --scan` to populate.", category: "—", liquidity: 0, volume_24h: 0, hours_to_resolution: null },
  ],
};

const pct = (v, d = 1) => `${(v * 100).toFixed(d)}%`;
const usd = (v) => `$${v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const ago = (iso) => {
  if (!iso) return "—";
  const s = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
};

const COLORS = {
  bg: "#0a0e17",
  card: "#111827",
  cardAlt: "#161f31",
  border: "#1e2a3a",
  borderBright: "#2a3f5f",
  text: "#c9d1d9",
  textDim: "#6b7b8d",
  textBright: "#e6edf3",
  accent: "#00d4aa",
  accentDim: "#00d4aa33",
  red: "#ff5c5c",
  redDim: "#ff5c5c22",
  yellow: "#f0b429",
  yellowDim: "#f0b42922",
  blue: "#58a6ff",
};

const edgeColor = (e) => e > 0.12 ? COLORS.accent : e > 0.08 ? COLORS.yellow : COLORS.textDim;
const pnlColor = (v) => v > 0 ? COLORS.accent : v < 0 ? COLORS.red : COLORS.text;

export default function Dashboard() {
  const [data, setData] = useState(MOCK_DATA);
  const [tab, setTab] = useState("opportunities");
  const [refreshing, setRefreshing] = useState(false);

  const loadData = useCallback(async () => {
    setRefreshing(true);
    try {
      const resp = await fetch("/dashboard_data.json");
      if (resp.ok) setData(await resp.json());
    } catch { /* use mock */ }
    setTimeout(() => setRefreshing(false), 400);
  }, []);

  useEffect(() => { loadData(); const t = setInterval(loadData, 30000); return () => clearInterval(t); }, [loadData]);

  const p = data.portfolio;
  const opps = data.opportunities || [];
  const openPos = data.open_positions || [];
  const closedPos = data.closed_positions || [];

  const cardStyle = {
    background: COLORS.card, border: `1px solid ${COLORS.border}`,
    borderRadius: 10, padding: "16px 20px",
  };
  const labelStyle = { fontSize: 11, color: COLORS.textDim, letterSpacing: "0.05em", textTransform: "uppercase", marginBottom: 4 };
  const valueStyle = { fontSize: 26, fontWeight: 700, fontFamily: "'JetBrains Mono', 'SF Mono', monospace" };
  const smallValueStyle = { fontSize: 18, fontWeight: 600, fontFamily: "'JetBrains Mono', 'SF Mono', monospace" };

  return (
    <div style={{ background: COLORS.bg, color: COLORS.text, minHeight: "100vh", fontFamily: "'Inter', -apple-system, sans-serif", padding: "24px 28px" }}>

      {/* ── Header ─────────────────── */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 28 }}>
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <div style={{ width: 10, height: 10, borderRadius: "50%", background: p.is_halted ? COLORS.red : COLORS.accent, boxShadow: `0 0 8px ${p.is_halted ? COLORS.red : COLORS.accent}` }} />
            <h1 style={{ fontSize: 22, fontWeight: 700, color: COLORS.textBright, margin: 0, letterSpacing: "-0.02em" }}>
              Prediction Market Trader
            </h1>
          </div>
          <div style={{ fontSize: 12, color: COLORS.textDim, marginTop: 4, marginLeft: 22 }}>
            {p.is_halted ? `⚠ HALTED: ${p.halt_reason}` : "System active"} · Last scan {ago(data.last_scan)}
          </div>
        </div>
        <button
          onClick={loadData}
          style={{
            background: "transparent", border: `1px solid ${COLORS.border}`, borderRadius: 8,
            color: COLORS.textDim, padding: "8px 16px", fontSize: 13, cursor: "pointer",
            transition: "all 0.2s", opacity: refreshing ? 0.5 : 1,
          }}
        >
          {refreshing ? "↻ Refreshing..." : "↻ Refresh"}
        </button>
      </div>

      {/* ── Stats Row ──────────────── */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))", gap: 12, marginBottom: 24 }}>
        <div style={cardStyle}>
          <div style={labelStyle}>Bankroll</div>
          <div style={{ ...valueStyle, color: COLORS.textBright }}>{usd(p.bankroll)}</div>
        </div>
        <div style={cardStyle}>
          <div style={labelStyle}>Total Return</div>
          <div style={{ ...valueStyle, color: pnlColor(p.total_return) }}>
            {p.total_return >= 0 ? "+" : ""}{pct(p.total_return)}
          </div>
        </div>
        <div style={cardStyle}>
          <div style={labelStyle}>Realized P&L</div>
          <div style={{ ...smallValueStyle, color: pnlColor(p.total_pnl_closed) }}>
            {p.total_pnl_closed >= 0 ? "+" : ""}{usd(p.total_pnl_closed)}
          </div>
        </div>
        <div style={cardStyle}>
          <div style={labelStyle}>Exposure</div>
          <div style={smallValueStyle}>{pct(p.exposure_pct, 0)}</div>
          <div style={{ fontSize: 11, color: COLORS.textDim }}>{usd(p.total_exposure)} deployed</div>
        </div>
        <div style={cardStyle}>
          <div style={labelStyle}>Drawdown</div>
          <div style={{ ...smallValueStyle, color: p.drawdown > 0.10 ? COLORS.red : p.drawdown > 0.05 ? COLORS.yellow : COLORS.text }}>
            {pct(p.drawdown)}
          </div>
        </div>
        <div style={cardStyle}>
          <div style={labelStyle}>Win Rate</div>
          <div style={smallValueStyle}>
            {p.closed_trades > 0 ? pct(p.win_rate, 0) : "—"}
          </div>
          <div style={{ fontSize: 11, color: COLORS.textDim }}>{p.wins}W / {p.losses}L</div>
        </div>
      </div>

      {/* ── Tabs ───────────────────── */}
      <div style={{ display: "flex", gap: 0, marginBottom: 16, borderBottom: `1px solid ${COLORS.border}` }}>
        {[
          { key: "opportunities", label: `Opportunities (${opps.length})` },
          { key: "positions", label: `Open (${openPos.length})` },
          { key: "history", label: `History (${closedPos.length})` },
        ].map((t2) => (
          <button
            key={t2.key}
            onClick={() => setTab(t2.key)}
            style={{
              background: "transparent", border: "none", borderBottom: `2px solid ${tab === t2.key ? COLORS.accent : "transparent"}`,
              color: tab === t2.key ? COLORS.textBright : COLORS.textDim,
              padding: "10px 20px", fontSize: 13, fontWeight: 600, cursor: "pointer",
              transition: "all 0.2s",
            }}
          >
            {t2.label}
          </button>
        ))}
      </div>

      {/* ── Opportunities ──────────── */}
      {tab === "opportunities" && (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {opps.length === 0 && <div style={{ padding: 40, textAlign: "center", color: COLORS.textDim }}>No opportunities found. Run a scan to populate.</div>}
          {opps.map((o, i) => (
            <div key={i} style={{ ...cardStyle, display: "grid", gridTemplateColumns: "1fr auto", gap: 16, alignItems: "start" }}>
              <div>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
                  <span style={{
                    fontSize: 10, fontWeight: 700, padding: "2px 8px", borderRadius: 4,
                    background: o.direction === "yes" ? COLORS.accentDim : COLORS.redDim,
                    color: o.direction === "yes" ? COLORS.accent : COLORS.red,
                    textTransform: "uppercase", letterSpacing: "0.08em",
                  }}>
                    {o.direction}
                  </span>
                  <span style={{ fontSize: 10, color: COLORS.textDim, padding: "2px 8px", borderRadius: 4, background: COLORS.cardAlt }}>
                    {o.category}
                  </span>
                  {o.hours_to_resolution && (
                    <span style={{ fontSize: 10, color: COLORS.textDim }}>
                      ⏱ {o.hours_to_resolution < 24 ? `${Math.round(o.hours_to_resolution)}h` : `${Math.round(o.hours_to_resolution / 24)}d`}
                    </span>
                  )}
                </div>
                <div style={{ fontSize: 14, fontWeight: 600, color: COLORS.textBright, marginBottom: 6, lineHeight: 1.4 }}>
                  {o.question}
                </div>
                <div style={{ fontSize: 12, color: COLORS.textDim, lineHeight: 1.5 }}>
                  {o.reasoning}
                </div>
              </div>
              <div style={{ textAlign: "right", minWidth: 140 }}>
                <div style={{ display: "flex", gap: 16, justifyContent: "flex-end", marginBottom: 8 }}>
                  <div>
                    <div style={{ fontSize: 10, color: COLORS.textDim }}>MARKET</div>
                    <div style={{ fontSize: 18, fontWeight: 700, fontFamily: "monospace" }}>{pct(o.market_price, 0)}</div>
                  </div>
                  <div style={{ color: COLORS.textDim, fontSize: 18, lineHeight: "28px" }}>→</div>
                  <div>
                    <div style={{ fontSize: 10, color: COLORS.textDim }}>ESTIMATE</div>
                    <div style={{ fontSize: 18, fontWeight: 700, fontFamily: "monospace", color: COLORS.accent }}>{pct(o.estimated_prob, 0)}</div>
                  </div>
                </div>
                <div style={{ display: "flex", gap: 12, justifyContent: "flex-end" }}>
                  <div style={{ textAlign: "right" }}>
                    <div style={{ fontSize: 10, color: COLORS.textDim }}>EDGE</div>
                    <div style={{ fontSize: 14, fontWeight: 700, fontFamily: "monospace", color: edgeColor(Math.abs(o.edge)) }}>
                      {o.edge >= 0 ? "+" : ""}{pct(o.edge)}
                    </div>
                  </div>
                  <div style={{ textAlign: "right" }}>
                    <div style={{ fontSize: 10, color: COLORS.textDim }}>SCORE</div>
                    <div style={{ fontSize: 14, fontWeight: 600, fontFamily: "monospace" }}>{o.score?.toFixed(1)}</div>
                  </div>
                  <div style={{ textAlign: "right" }}>
                    <div style={{ fontSize: 10, color: COLORS.textDim }}>SIZE</div>
                    <div style={{ fontSize: 14, fontWeight: 600, fontFamily: "monospace" }}>
                      {o.sizing?.total_cost ? usd(o.sizing.total_cost) : "—"}
                    </div>
                  </div>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* ── Open Positions ─────────── */}
      {tab === "positions" && (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {openPos.length === 0 && <div style={{ padding: 40, textAlign: "center", color: COLORS.textDim }}>No open positions.</div>}
          {openPos.map((pos, i) => (
            <div key={i} style={{ ...cardStyle, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <div>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
                  <span style={{
                    fontSize: 10, fontWeight: 700, padding: "2px 8px", borderRadius: 4,
                    background: pos.direction === "yes" ? COLORS.accentDim : COLORS.redDim,
                    color: pos.direction === "yes" ? COLORS.accent : COLORS.red,
                    textTransform: "uppercase",
                  }}>
                    {pos.direction}
                  </span>
                  <span style={{ fontSize: 10, color: COLORS.textDim }}>{pos.category}</span>
                </div>
                <div style={{ fontSize: 13, fontWeight: 600, color: COLORS.textBright }}>{pos.question}</div>
                <div style={{ fontSize: 11, color: COLORS.textDim, marginTop: 4 }}>
                  Opened {ago(pos.opened_at)} · Entry edge: {pct(Math.abs(pos.edge_at_entry))}
                </div>
              </div>
              <div style={{ textAlign: "right" }}>
                <div style={{ fontSize: 11, color: COLORS.textDim }}>COST</div>
                <div style={{ fontSize: 16, fontWeight: 700, fontFamily: "monospace" }}>{usd(pos.total_cost)}</div>
                <div style={{ fontSize: 11, color: COLORS.textDim, marginTop: 4 }}>
                  {pos.num_shares} shares @ {usd(pos.entry_price)}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* ── Trade History ──────────── */}
      {tab === "history" && (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {closedPos.length === 0 && <div style={{ padding: 40, textAlign: "center", color: COLORS.textDim }}>No closed trades yet.</div>}
          {[...closedPos].reverse().map((pos, i) => (
            <div key={i} style={{ ...cardStyle, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <div>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
                  <span style={{
                    fontSize: 10, fontWeight: 700, padding: "2px 8px", borderRadius: 4,
                    background: (pos.pnl || 0) >= 0 ? COLORS.accentDim : COLORS.redDim,
                    color: (pos.pnl || 0) >= 0 ? COLORS.accent : COLORS.red,
                  }}>
                    {(pos.pnl || 0) >= 0 ? "WIN" : "LOSS"}
                  </span>
                  <span style={{ fontSize: 10, color: COLORS.textDim }}>{pos.direction?.toUpperCase()}</span>
                </div>
                <div style={{ fontSize: 13, fontWeight: 600, color: COLORS.textBright }}>{pos.question}</div>
                <div style={{ fontSize: 11, color: COLORS.textDim, marginTop: 4 }}>
                  Closed {ago(pos.closed_at)} · Held since {ago(pos.opened_at)}
                </div>
              </div>
              <div style={{ textAlign: "right" }}>
                <div style={{ fontSize: 11, color: COLORS.textDim }}>P&L</div>
                <div style={{ fontSize: 18, fontWeight: 700, fontFamily: "monospace", color: pnlColor(pos.pnl || 0) }}>
                  {(pos.pnl || 0) >= 0 ? "+" : ""}{usd(pos.pnl || 0)}
                </div>
                <div style={{ fontSize: 11, color: pnlColor(pos.return_pct || 0), marginTop: 2 }}>
                  {(pos.return_pct || 0) >= 0 ? "+" : ""}{pct(pos.return_pct || 0)}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* ── Footer ─────────────────── */}
      <div style={{ marginTop: 32, padding: "16px 0", borderTop: `1px solid ${COLORS.border}`, fontSize: 11, color: COLORS.textDim, display: "flex", justifyContent: "space-between" }}>
        <span>Data updates every 30s · Dashboard reads from dashboard_data.json</span>
        <span>Updated {ago(data.updated_at)}</span>
      </div>
    </div>
  );
}
