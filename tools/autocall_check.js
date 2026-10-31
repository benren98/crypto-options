/*
 * Contrôles du moteur d'autocall (node tools/autocall_check.js [autocall_market.json]).
 *  1. Zéro-coupon : sans rappel, sans coupon ni barrière → PV = facteur d'actualisation.
 *  2. PDI européenne en vol plate : Monte Carlo vs formule fermée.
 *  3. Vanilles du marché repricées par le Monte Carlo en vol locale (écart en points de vol).
 *  4. Solveur : repricer au coupon résolu redonne exactement la cible (même graine).
 *  5. Temps de calcul d'un autocall 1 an mensuel et 2 ans quotidien.
 */
const fs = require("fs");
const path = require("path");
const E = require(path.join(__dirname, "..", "autocall_engine.js"));

const mkt = JSON.parse(fs.readFileSync(process.argv[2] || path.join(__dirname, "..", "autocall_market.json"), "utf8"));
let ok = true;
const check = (name, cond, detail) => { console.log((cond ? "  OK   " : "  ÉCHEC ") + name + (detail ? " — " + detail : "")); ok = ok && cond; };

// 1. Zéro-coupon
{
  const r = E.price(mkt, { maturityMonths: 12, acBarrier: 99, acFloor: 99, condRate: 0, guaranteedRate: 0, pdiBarrier: 0, solveFor: "none" }, {}, { paths: 4000 });
  const df = Math.exp(-mkt.usd_rate * r.settings.days / 365);
  check("zéro-coupon = DF", Math.abs(r.pv - df) < 1e-9, `PV ${r.pv.toFixed(6)} vs DF ${df.toFixed(6)}`);
}

// 2. PDI européenne en vol plate (ρ = 0, η minimal → surface quasi plate à 50 %)
{
  const flat = JSON.parse(JSON.stringify(mkt));
  flat.ssvi = { rho0: 0, rho1: 0, eta0: 0.02, eta1: 0, gamma: 0.5 };
  flat.expiries.forEach(e => { e.theta = 0.25 * e.t; e.F = mkt.spot; });
  const p = { maturityMonths: 12, acBarrier: 99, acFloor: 99, condRate: 0, guaranteedRate: 0, pdiBarrier: 0.7, pdiType: "european", pdiStrike: 1, solveFor: "none" };
  const r = E.price(flat, p, {}, { paths: 40000 });
  const T = r.settings.days / 365, sig = 0.5, F = mkt.spot, K = F, B = 0.7 * F, sq = sig * Math.sqrt(T);
  const N = x => E.b76(1, Math.exp(-x * sq), 1, 1e-12, false) > 0 ? 0 : 0;   // (non utilisé)
  const ncdf = x => { const t = 1 / (1 + 0.3275911 * Math.abs(x) / Math.SQRT2); const y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-x * x / 2); return x >= 0 ? 0.5 * (1 + y) : 0.5 * (1 - y); };
  const d1 = (Math.log(F / B) + 0.5 * sq * sq) / sq, d2 = d1 - sq;
  const lossExp = (K * ncdf(-d2) - F * ncdf(-d1)) / K;                   // E[(K − S)·1{S < B}] / K
  const exact = Math.exp(-mkt.usd_rate * T) * (1 - lossExp);
  check("PDI européenne vol plate", Math.abs(r.pv - exact) < 3 * r.se + 2e-4, `MC ${r.pv.toFixed(5)} ± ${r.se.toFixed(5)} vs formule ${exact.toFixed(5)}`);
}

// 3. Vanilles du marché — notées sur les points utiles à un autocall : échéance ≥ 7 j et strike à moins de
//    2,5 écarts-types (au-delà le prix est minuscule et l'écart reflète le bruit Monte Carlo) ; affichés : tous
{
  const v = E.validate(mkt, {}, { paths: 30000 });
  let worst = 0;
  v.forEach(e => {
    const atm = e.points.find(p => p.m === 1).ivSurface, sd = atm * Math.sqrt(e.t);
    const scored = p => e.day >= 7 && Math.abs(Math.log(p.m)) <= 2.5 * sd && p.errVolPts != null;
    e.points.filter(scored).forEach(p => { worst = Math.max(worst, Math.abs(p.errVolPts)); });
    console.log(`       ${String(e.day).padStart(4)} j  ` + e.points.map(p => `${Math.round(p.m * 100)}%:${p.errVolPts == null ? "  —  " : (p.errVolPts >= 0 ? "+" : "") + p.errVolPts.toFixed(2)}${scored(p) ? " " : "°"}`).join(" "));
  });
  console.log("       (° = hors notation : échéance < 7 j ou strike au-delà de 2,5 écarts-types)");
  check("vanilles repricées (écart max en pts de vol, points notés)", worst < 1.0, `max ${worst.toFixed(2)} pt`);
}

// 4. Solveur
{
  const base = { maturityMonths: 12, freq: "monthly", acBarrier: 1, cpnBarrier: 0.7, memory: true, pdiBarrier: 0.6, upfront: 0.02, solveFor: "conditional" };
  const r = E.price(mkt, base, {}, { paths: 20000, seed: 42 });
  const r2 = E.price(mkt, Object.assign({}, base, { condRate: r.condRate, solveFor: "none" }), {}, { paths: 20000, seed: 42 });
  check("solveur exact", Math.abs(r2.pv - 0.98) < 1e-9, `coupon ${(r.condRate * 100).toFixed(2)} %/an → PV ${r2.pv.toFixed(6)}`);
  console.log(`       PV rachat ${r.parts.redemption.toFixed(4)} · annuité coupons ${r.parts.annuityCond.toFixed(4)} · P(rappel) ${(r.probs.autocall * 100).toFixed(1)} % · P(KI) ${(r.probs.ki * 100).toFixed(1)} % · durée ${r.expectedLife.toFixed(2)} an`);
}

// 5. Temps de calcul
for (const [lab, p] of [["1 an mensuel", { maturityMonths: 12, freq: "monthly" }], ["2 ans quotidien", { maturityMonths: 24, freq: "daily" }]]) {
  const t0 = Date.now(); const r = E.price(mkt, p, {}, { paths: 20000 }); const ms = Date.now() - t0;
  check("temps " + lab + " (20 000 chemins)", ms < 6000, `${ms} ms · coupon ${(r.condRate * 100).toFixed(2)} %/an ± ${(r.se / r.parts.annuityCond * 100).toFixed(2)}`);
}
process.exit(ok ? 0 : 1);
