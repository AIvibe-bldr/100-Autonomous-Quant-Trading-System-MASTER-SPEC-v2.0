// Shared SAMPLE data and tiny helpers for the UI mockups. Everything here is made up for design review.
// Real pages would fill the same shapes from /api/status, /api/performance, /api/sessions, /api/observers ...
"use strict";
const STATE = new URLSearchParams(location.search).get("state") || "ok";     // ok | warn | bad

// 18 plausible daily equity points (deterministic, not real results)
const EQUITY = (() => { const out = []; let v = 670, s = 7;
  for (let i = 0; i < 18; i++) { s = (s * 9301 + 49297) % 233280; v *= 1 + ((s / 233280) - 0.47) * 0.012; out.push(+v.toFixed(2)); }
  out[17] = 672.85; return out; })();
// the 18 weekdays ending on the sample "last session" day (8/14)
const DAYS = (() => { const out = []; const d = new Date(2026, 7, 14);
  while (out.length < 18) { if (d.getDay() % 6) out.unshift((d.getMonth() + 1) + "/" + d.getDate()); d.setDate(d.getDate() - 1); } return out; })();

const BASE = {
  equity: 672.85, start: 670.00, ret: 0.0042, maxDD: 0.0112, sessions: 18, needSessions: 60,
  up: 9, down: 8, flat: 1, fees: 0.06, llmCost: null,
  lastSession: "8/14", nextSession: "11/10（火）23:45", heartbeat: "30秒前",
  today: { bought: ["TSLA", "PANW"], stops: 0, skipped: 8, pnl: 10.33 },
  reasons: ["PLTR：資金に対して1株の大きさが小さすぎるため見送り", "AMD：同上", "NVDA：AIは買いと判断したが、保有上限に近いため見送り",
            "AVGO：買い候補（確信度 0.62）だが、見解が割れていたため購入量を2割減らした"],
  ai: { forecast: "記録 38 件 · 結果が出たもの 0 件（結果が出るまで1週間〜）", shadow: "AI あり -1.07%：AIなしの機械的な買いより今のところ劣る（まだ少数日）",
        humanVsAi: "人の介入 0 件", cells: "AI／人の帳簿とマスターの照合：4回すべて一致" },
  sessionsList: [
    { d: "8/14", pnl: 10.33, note: "2件買い（TSLA・PANW）", bad: false }, { d: "8/13", pnl: -5.95, note: "3件買い・損切り0", bad: false },
    { d: "8/12", pnl: -1.37, note: "5件買い", bad: false }, { d: "8/11", pnl: -0.16, note: "5件買い", bad: false } ],
};
const STATES = {
  ok:   { level: "ok",   word: "正常",   sentence: "正常に動いています。", detail: "次の取引は 11/10（火）23:45 です。", alerts: [], safety: "通常（新規の買いは許可）", runner: "待機中" },
  warn: { level: "warn", word: "注意",   sentence: "動いていますが、確認してほしいことがあります。", detail: "安全装置が新規の買いを止めています。決済と損切りは動いています。",
          alerts: ["安全装置が新規の買いを止めました（ブローカーとの照合で不一致）", "ニュースの取得に1回失敗しました（取引には影響なし）"], safety: "新規の買いを停止中", runner: "待機中" },
  bad:  { level: "bad",  word: "停止",   sentence: "止まっています。対応が必要です。", detail: "12分前から応答がありません。未解決の重大な障害が1件あります。",
          alerts: ["実行器の応答が12分途絶えています", "未解決の重大障害 INC-0001（セッション中に例外）"], safety: "停止（人の確認待ち）", runner: "応答なし" },
};
const S = STATES[STATE] || STATES.ok;

const money = (x, sign) => (sign && x > 0 ? "+" : x < 0 ? "−" : "") + "$" + Math.abs(x).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const pct = (x, d = 2, sign = true) => (sign && x > 0 ? "+" : x < 0 ? "−" : "") + Math.abs(x * 100).toFixed(d) + "%";
const tone = (x) => x > 0 ? "up" : x < 0 ? "down" : "";

// Inline SVG line/area chart from numbers only (no data goes into markup as text).
function chart(values, { w = 600, h = 160, stroke = "#2563eb", fill = "rgba(37,99,235,.10)", pad = 6, area = true, dot = true } = {}) {
  const lo = Math.min(...values), hi = Math.max(...values), r = hi - lo || 1;
  const x = (i) => pad + (w - 2 * pad) * i / (values.length - 1), y = (v) => pad + (h - 2 * pad) * (1 - (v - lo) / r);
  const pts = values.map((v, i) => x(i).toFixed(1) + "," + y(v).toFixed(1)).join(" ");
  const last = values.length - 1;
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" width="100%" height="${h}" role="img" aria-label="資産の推移">` +
    (area ? `<polygon points="${pad},${h - pad} ${pts} ${w - pad},${h - pad}" fill="${fill}"/>` : "") +
    `<polyline points="${pts}" fill="none" stroke="${stroke}" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>` +
    (dot ? `<circle cx="${x(last).toFixed(1)}" cy="${y(values[last]).toFixed(1)}" r="4" fill="${stroke}"/>` : "") + `</svg>`;
}
function $(id) { return document.getElementById(id); }
function stateSwitcher() {
  return `<div class="mock-switch">状態の見本：` + ["ok", "warn", "bad"].map((k) =>
    `<a href="?state=${k}" ${k === STATE ? 'class="on"' : ""}>${{ ok: "正常", warn: "注意", bad: "停止" }[k]}</a>`).join("") + `<span>※数値はすべてサンプル</span></div>`;
}
