/*
 * autocall_engine.js — Pricer d'autocall sur BTC (Monte Carlo en volatilité locale).
 *
 * Utilisé par docs/autocall.html (dans un Web Worker) et par tools/autocall_check.js (Node).
 * Aucune dépendance, aucun accès au DOM.
 *
 * Marché (autocall_market.json, calibré par autocall_market.py) :
 *   surface SSVI w(k, t) = θ_t/2 · (1 + ρφk + √((φk + ρ)² + 1 − ρ²)), k = ln(K/F_t), φ = η/(θ^γ (1+θ)^(1−γ)),
 *   ρ(θ) = ρ0 + ρ1√θ, η(θ) = η0 + η1√θ ; θ_t interpolé linéairement entre échéances, ATM constante au-delà ;
 *   forwards F_t des futures synthétiques Deribit (base interpolée), actualisation au taux USD.
 * Marges du vendeur sur le modèle : décalage parallèle de vol, facteur de skew (ρ), facteur d'ailes (η),
 *   décalage de la base des forwards, taux USD.
 * Modèle de diffusion : volatilité locale de Dupire tirée de la surface (reprice les vanilles),
 *   schéma d'Euler en log-forward, pas journalier (le BTC cote 7 j/7), variables antithétiques.
 * Le prix de l'autocall est linéaire en chaque taux de coupon : une seule simulation donne
 *   PV = A + c·Bc + g·Bg, d'où le coupon qui égalise PV à 100 % − marge du vendeur.
 */
(function (root) {
  "use strict";

  const DAY = 1 / 365;
  const K_MIN = -4.0, K_MAX = 2.5, K_STEP = 0.02;            // grille de log-moneyness de la vol locale
  const NK = Math.round((K_MAX - K_MIN) / K_STEP) + 1;

  // ── Loi normale ──────────────────────────────────────────────────────────
  function ncdf(x) {                                            // Abramowitz-Stegun 7.1.26 (erreur < 1e-7)
    const t = 1 / (1 + 0.3275911 * Math.abs(x) / Math.SQRT2);
    const y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t
      * Math.exp(-x * x / 2);
    return x >= 0 ? 0.5 * (1 + y) : 0.5 * (1 - y);
  }
  function b76(F, K, T, sig, call) {                            // prix forward (non actualisé)
    if (T <= 0 || sig <= 0) return call ? Math.max(F - K, 0) : Math.max(K - F, 0);
    const sq = sig * Math.sqrt(T), d1 = (Math.log(F / K) + 0.5 * sq * sq) / sq, d2 = d1 - sq;
    return call ? F * ncdf(d1) - K * ncdf(d2) : K * ncdf(-d2) - F * ncdf(-d1);
  }
  function impliedVol(price, F, K, T, call) {
    const intr = call ? Math.max(F - K, 0) : Math.max(K - F, 0);
    if (!(price > intr + 1e-12) || price >= (call ? F : K)) return null;
    let lo = 1e-3, hi = 6;
    for (let i = 0; i < 90; i++) { const m = 0.5 * (lo + hi); if (b76(F, K, T, m, call) > price) hi = m; else lo = m; }
    return 0.5 * (lo + hi);
  }

  // ── Générateur pseudo-aléatoire reproductible (mulberry32) + Box-Muller ──
  function rng(seed) {
    let a = seed >>> 0, spare = null;
    const u = () => { a = (a + 0x6D2B79F5) >>> 0; let t = a; t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
    return function normal() {
      if (spare !== null) { const s = spare; spare = null; return s; }
      let x, y, r;
      do { x = 2 * u() - 1; y = 2 * u() - 1; r = x * x + y * y; } while (r >= 1 || r === 0);
      const f = Math.sqrt(-2 * Math.log(r) / r);
      spare = y * f; return x * f;
    };
  }

  // ── Surface (avec marges) ────────────────────────────────────────────────
  const DEFAULT_MARGINS = { volShift: 0, skewMult: 1, wingMult: 1, basisShift: 0, rate: null };

  function makeSurface(mkt, margins) {
    const m = Object.assign({}, DEFAULT_MARGINS, margins || {});
    const P = mkt.ssvi, S0 = mkt.spot;
    const ex = mkt.expiries.slice().sort((a, b) => a.t - b.t);
    const ts = [0].concat(ex.map(e => e.t)), ths = [0].concat(ex.map(e => e.theta));
    const bas = ex.map(e => Math.log(e.F / S0) / e.t);         // base annualisée (log) par échéance
    const tLast = ts[ts.length - 1], thLast = ths[ths.length - 1];
    const rate = (m.rate == null ? mkt.usd_rate : m.rate);

    function theta(t) {
      if (t >= tLast) return thLast * t / tLast;                // vol ATM constante au-delà de la dernière échéance
      let i = 1; while (ts[i] < t) i++;
      return ths[i - 1] + (ths[i] - ths[i - 1]) * (t - ts[i - 1]) / (ts[i] - ts[i - 1]);
    }
    function basis(t) {
      if (t <= ex[0].t) return bas[0];
      if (t >= ex[ex.length - 1].t) return bas[bas.length - 1];
      let i = 1; while (ex[i].t < t) i++;
      return bas[i - 1] + (bas[i] - bas[i - 1]) * (t - ex[i - 1].t) / (ex[i].t - ex[i - 1].t);
    }
    function wBase(k, t) {
      const th = Math.max(theta(t), 1e-10), sq = Math.sqrt(th);
      let rho = Math.max(-0.98, Math.min(0.98, (P.rho0 + P.rho1 * sq) * m.skewMult));
      let eta = Math.max(0.02, (P.eta0 + P.eta1 * sq) * m.wingMult);
      eta = Math.min(eta, 2 / (1 + Math.abs(rho)) - 1e-6);      // condition sans arbitrage papillon
      const phi = eta / (Math.pow(th, P.gamma) * Math.pow(1 + th, 1 - P.gamma));
      return th / 2 * (1 + rho * phi * k + Math.sqrt((phi * k + rho) * (phi * k + rho) + 1 - rho * rho));
    }
    function vol(k, t) {                                        // vol implicite avec décalage parallèle
      return Math.max(0.01, Math.sqrt(wBase(k, t) / t) + m.volShift / 100);
    }
    function w(k, t) { const s = vol(k, t); return s * s * t; }
    return {
      S0, rate, margins: m, vol, w,
      F: t => S0 * Math.exp((basis(t) + m.basisShift / 100) * t),
      DF: t => Math.exp(-rate * t),
    };
  }

  // Volatilité locale de Dupire en variance totale : σ² = ∂t w / [1 − k w'/w + ¼(−¼ − 1/w + k²/w²) w'² + ½ w'']
  function localVol(surf, k, t) {
    const dt = Math.min(1e-4, t / 2), dk = 1e-3;
    const w0 = surf.w(k, t), wt = (surf.w(k, t + dt) - surf.w(k, t - dt)) / (2 * dt);
    const wp = surf.w(k + dk, t), wm = surf.w(k - dk, t);
    const wk = (wp - wm) / (2 * dk), wkk = (wp - 2 * w0 + wm) / (dk * dk);
    const den = 1 - k * wk / w0 + 0.25 * (-0.25 - 1 / w0 + k * k / (w0 * w0)) * wk * wk + 0.5 * wkk;
    let v = den > 1e-6 && wt > 0 ? wt / den : w0 / t;           // repli : vol implicite si arbitrage local
    return Math.min(4, Math.max(0.05, Math.sqrt(v)));
  }
  // Une ligne par jour : variance locale MOYENNÉE sur la journée (4 points) — une échéance listée
  // tombe en cours de journée et la variance peut sauter d'une échéance à l'autre (événement coté)
  const SUBT = [0.125, 0.375, 0.625, 0.875];
  function localVolGrid(surf, nDays) {
    const g = new Float32Array(nDays * NK);
    for (let n = 0; n < nDays; n++) {
      for (let j = 0; j < NK; j++) {
        let v = 0;
        for (const f of SUBT) { const s = localVol(surf, K_MIN + j * K_STEP, (n + f) * DAY); v += s * s; }
        g[n * NK + j] = Math.sqrt(v / SUBT.length);
      }
    }
    return g;
  }

  // Pas de temps : 4 sous-pas par jour sur les 30 premiers jours (les ailes des échéances courtes ne se
  // forment pas en quelques pas journaliers), 1 pas par jour ensuite. off[n] = indice du 1er aléa du jour n.
  const EARLY_DAYS = 30, EARLY_SUB = 4;
  function stepPlan(D) {
    const off = new Int32Array(D + 1);
    for (let n = 0; n < D; n++) off[n + 1] = off[n] + (n < EARLY_DAYS ? EARLY_SUB : 1);
    return off;
  }
  function advance(x, lv, n, off, zs, sgn) {           // fait avancer ln(S/F) d'un jour (vol locale du jour n)
    const nsub = off[n + 1] - off[n], dt = DAY / nsub, sq = Math.sqrt(dt), row = n * NK;
    for (let q = 0; q < nsub; q++) {
      let pos = (x - K_MIN) / K_STEP;
      if (pos < 0) pos = 0; else if (pos > NK - 1.001) pos = NK - 1.001;
      const j = pos | 0, fr = pos - j, s = lv[row + j] * (1 - fr) + lv[row + j + 1] * fr;
      x += -0.5 * s * s * dt + s * sq * sgn * zs[off[n] + q];
    }
    return x;
  }

  // ── Produit ──────────────────────────────────────────────────────────────
  const FREQ_DAYS = { daily: 1, weekly: 7, monthly: 365 / 12, quarterly: 365 / 4, semiannual: 365 / 2, annual: 365 };
  const DEFAULT_PRODUCT = {
    maturityMonths: 12, freq: "monthly", firstCallObs: 1,
    acBarrier: 1.00, acStepDown: 0.0, acFloor: 0.5,
    guaranteedRate: 0.0, guaranteedPeriods: 0,                  // 0 = sur toutes les périodes
    condRate: 0.20, cpnBarrier: 0.70, memory: true,
    pdiBarrier: 0.60, pdiType: "continuous", pdiStrike: 1.00,
    solveFor: "conditional", upfront: 0.02,                     // marge du vendeur, en % du nominal
  };
  function schedule(p) {
    const D = Math.max(1, Math.round(p.maturityMonths * 365 / 12)), per = FREQ_DAYS[p.freq] || FREQ_DAYS.monthly;
    const days = [];
    for (let i = 1; ; i++) { const d = Math.round(i * per); if (d >= D) break; if (!days.length || d > days[days.length - 1]) days.push(d); }
    days.push(D);
    return days;
  }

  // ── Monte Carlo de l'autocall ────────────────────────────────────────────
  function price(mkt, product, margins, opts) {
    const p = Object.assign({}, DEFAULT_PRODUCT, product || {});
    const o = Object.assign({ paths: 20000, seed: 12345 }, opts || {});
    const surf = makeSurface(mkt, margins);
    const days = schedule(p), nObs = days.length, D = days[nObs - 1];
    const lv = localVolGrid(surf, D);
    const Fd = new Float64Array(D + 1), DFd = new Float64Array(D + 1);
    for (let n = 0; n <= D; n++) { Fd[n] = surf.F(n * DAY); DFd[n] = surf.DF(n * DAY); }
    const obsAt = new Int32Array(D + 1).fill(-1);
    days.forEach((d, i) => { obsAt[d] = i; });
    const alpha = days.map((d, i) => (d - (i ? days[i - 1] : 0)) / 365);
    const acLevel = days.map((_, i) => Math.max(p.acFloor, p.acBarrier - p.acStepDown * Math.max(0, i + 1 - p.firstCallObs)));
    const gN = p.guaranteedPeriods > 0 ? p.guaranteedPeriods : nObs;          // périodes à coupon garanti
    const condFrom = p.guaranteedPeriods > 0 && p.guaranteedRate > 0 ? p.guaranteedPeriods : 0;
    const S0 = surf.S0, off = stepPlan(D);

    const N = o.paths - (o.paths % 2), normal = rng(o.seed);
    const A = new Float64Array(N), Bc = new Float64Array(N), Bg = new Float64Array(N);
    const callCount = new Float64Array(nObs);
    let ki = 0, lossN = 0, lossSum = 0, lifeSum = 0, cpnUnits = 0;
    // Issues : rappel anticipé · échéance sans perte · pertes en capital par tranche
    const OUT = [["Rappelé avant l'échéance", null], ["Échéance, capital rendu", 0], ["Perte 0–10 %", 0.10],
                 ["Perte 10–20 %", 0.20], ["Perte 20–40 %", 0.40], ["Perte 40–60 %", 0.60], ["Perte > 60 %", Infinity]];
    const hist = new Float64Array(OUT.length);
    const zs = new Float64Array(off[D]);

    for (let path = 0; path < N; path++) {
      if (path % 2 === 0) for (let n = 0; n < off[D]; n++) zs[n] = normal();
      const sgn = path % 2 === 0 ? 1 : -1;                      // antithétique
      let x = 0, minRatio = 1, a = 0, bc = 0, bg = 0, bank = 0, alive = true, ratio = 1;
      for (let n = 0; n < D && alive; n++) {
        x = advance(x, lv, n, off, zs, sgn);
        ratio = Fd[n + 1] * Math.exp(x) / S0;
        if (ratio < minRatio) minRatio = ratio;
        const i = obsAt[n + 1];
        if (i < 0) continue;
        const df = DFd[n + 1];
        if (i < gN && p.guaranteedRate > 0) bg += df * alpha[i];
        if (i >= condFrom) {
          if (ratio >= p.cpnBarrier) { bc += df * (alpha[i] + bank); cpnUnits += 1; bank = 0; }
          else if (p.memory) bank += alpha[i];
        }
        if (i < nObs - 1 && i + 1 >= p.firstCallObs && ratio >= acLevel[i]) {
          a += df; alive = false; callCount[i] += 1; lifeSum += days[i] * DAY;
        }
      }
      if (alive) {                                                  // échéance
        const hit = p.pdiType === "european" ? ratio <= p.pdiBarrier : minRatio <= p.pdiBarrier;
        let red = 1;
        if (hit) { ki += 1; if (ratio < p.pdiStrike) { const loss = (p.pdiStrike - ratio) / p.pdiStrike; red = 1 - loss; lossN += 1; lossSum += loss; } }
        a += DFd[D] * red; callCount[nObs - 1] += 1; lifeSum += D * DAY;
        const loss = 1 - red;
        let b = 1; if (loss > 1e-12) { b = 2; while (b < OUT.length - 1 && loss > OUT[b][1]) b++; }
        hist[b] += 1;
      } else hist[0] += 1;
      A[path] = a; Bc[path] = bc; Bg[path] = bg;
    }

    const mean = arr => { let s = 0; for (let i = 0; i < arr.length; i++) s += arr[i]; return s / arr.length; };
    const mA = mean(A), mBc = mean(Bc), mBg = mean(Bg), target = 1 - p.upfront;
    let c = p.condRate, g = p.guaranteedRate;
    if (p.solveFor === "conditional" && mBc > 1e-9) c = (target - mA - g * mBg) / mBc;
    if (p.solveFor === "guaranteed" && mBg > 1e-9) g = (target - mA - c * mBc) / mBg;
    // Erreur type sur les paires antithétiques (indépendantes entre elles)
    let s1 = 0, s2 = 0;
    for (let i = 0; i < N; i += 2) {
      const v = 0.5 * (A[i] + c * Bc[i] + g * Bg[i] + A[i + 1] + c * Bc[i + 1] + g * Bg[i + 1]);
      s1 += v; s2 += v * v;
    }
    const pairs = N / 2, pv = s1 / pairs, se = Math.sqrt(Math.max(0, s2 / pairs - pv * pv) / pairs);
    return {
      pv, se, sellerMargin: 1 - pv, condRate: c, guaranteedRate: g, target,
      parts: { redemption: mA, conditional: c * mBc, guaranteed: g * mBg, annuityCond: mBc, annuityGuar: mBg,
               zeroCoupon: DFd[D] },
      obs: days.map((d, i) => ({ day: d, t: d * DAY, alpha: alpha[i], acLevel: i + 1 >= p.firstCallObs && i < nObs - 1 ? acLevel[i] : null,
                                  callProb: callCount[i] / N })),
      probs: { autocall: 1 - callCount[nObs - 1] / N, maturity: callCount[nObs - 1] / N, ki: ki / N,
               loss: lossN / N, lossGivenLoss: lossN ? lossSum / lossN : 0 },
      expectedLife: lifeSum / N, expectedCoupons: cpnUnits / N,
      outcomes: OUT.map((o, i) => ({ label: o[0], p: hist[i] / N })),
      settings: { paths: N, seed: o.seed, days: D, nObs, rate: surf.rate, margins: surf.margins },
    };
  }

  // ── Contrôle : les vanilles du marché repricées par le Monte Carlo ───────
  function validate(mkt, margins, opts) {
    const o = Object.assign({ paths: 20000, seed: 777, maxDays: 400, moneyness: [0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.4] }, opts || {});
    const surf = makeSurface(mkt, margins);
    const exDays = Array.from(new Set(mkt.expiries.map(e => Math.max(1, Math.round(e.t * 365))).filter(d => d <= o.maxDays))).sort((a, b) => a - b);
    const D = exDays[exDays.length - 1], lv = localVolGrid(surf, D);
    const Fd = new Float64Array(D + 1); for (let n = 0; n <= D; n++) Fd[n] = surf.F(n * DAY);
    const at = new Int32Array(D + 1).fill(-1); exDays.forEach((d, i) => { at[d] = i; });
    const N = o.paths - (o.paths % 2), normal = rng(o.seed), off = stepPlan(D);
    const sums = exDays.map(() => new Float64Array(o.moneyness.length));
    const zs = new Float64Array(off[D]);
    for (let path = 0; path < N; path++) {
      if (path % 2 === 0) for (let n = 0; n < off[D]; n++) zs[n] = normal();
      const sgn = path % 2 === 0 ? 1 : -1;
      let x = 0;
      for (let n = 0; n < D; n++) {
        x = advance(x, lv, n, off, zs, sgn);
        const e = at[n + 1];
        if (e >= 0) {
          const S = Fd[n + 1] * Math.exp(x);
          o.moneyness.forEach((m, q) => { const K = m * Fd[n + 1]; sums[e][q] += m < 1 ? Math.max(K - S, 0) : Math.max(S - K, 0); });
        }
      }
    }
    return exDays.map((d, e) => {
      const t = d * DAY, F = Fd[d];
      return { day: d, t, F, points: o.moneyness.map((m, q) => {
        const K = m * F, call = m >= 1, mc = sums[e][q] / N;
        const ivMkt = surf.vol(Math.log(m), t), ivMc = impliedVol(mc, F, K, t, call);
        return { m, K, ivSurface: ivMkt, ivMc, errVolPts: ivMc == null ? null : (ivMc - ivMkt) * 100 };
      }) };
    });
  }

  const API = { price, validate, makeSurface, localVol, schedule, b76, impliedVol, DEFAULT_PRODUCT, DEFAULT_MARGINS, FREQ_DAYS };
  if (typeof module !== "undefined" && module.exports) module.exports = API;
  else root.AutocallEngine = API;
})(typeof self !== "undefined" ? self : this);
