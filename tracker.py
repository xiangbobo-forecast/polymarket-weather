#!/usr/bin/env python3
"""Polymarket "highest temperature" PAPER-TRADING tracker.

Paper only: no wallet, no keys, no orders. Reads public data:
  - Polymarket Gamma API (events/markets) and CLOB order books
  - aviationweather.gov (station coordinates, METAR observations)
  - Open-Meteo ensemble API (GFS / ECMWF IFS / ICON members)
Writes: data/state.json, data/trades.csv, data/snapshots/<date>/<time>.csv.gz,
        data/summary.json, REPORT.md
Standard library only.
"""
import csv, gzip, io, json, math, os, re, sys, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
CFG = json.load(open(os.path.join(ROOT, "config.json")))
UA = {"User-Agent": "Mozilla/5.0 (polymarket-weather paper tracker)", "Accept-Encoding": "gzip"}
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
AWC = "https://aviationweather.gov/api/data"
ENS = "https://ensemble-api.open-meteo.com/v1/ensemble"
NOW = datetime.now(timezone.utc)
LOG = []


def log(msg):
    print(msg, flush=True)
    LOG.append(msg)


def http(url, body=None, timeout=60, tries=3):
    last = None
    for i in range(tries):
        try:
            data = json.dumps(body).encode() if body is not None else None
            h = dict(UA)
            if data is not None:
                h["Content-Type"] = "application/json"
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h), timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (400, 401, 403, 404, 429):  # don't hammer on quota / auth errors
                raise
        except Exception as e:  # network hiccup
            last = e
        time.sleep(2 * (i + 1))
    raise last


# ---------------------------------------------------------------- parsing
def parse_bucket(label):
    """Return (lo, hi, unit) in whole degrees; None = open end."""
    s = label.replace("−", "-").strip()
    unit = "F" if "°F" in s else "C"
    n = [int(x) for x in re.findall(r"-?\d+", s.replace("°", " "))]
    if "or below" in s:
        return (None, n[0], unit)
    if "or higher" in s or "or above" in s:
        return (n[0], None, unit)
    m = re.match(r"\s*(-?\d+)\s*-\s*(-?\d+)", s)
    if m:
        return (int(m.group(1)), int(m.group(2)), unit)
    return (n[0], n[0], unit)


def bucket_bounds_c(lo, hi, unit):
    """Continuous °C interval [a, b) whose rounded (half-up) value falls in the bucket."""
    def f2c(f):
        return (f - 32) * 5 / 9
    a = -math.inf if lo is None else (lo - 0.5 if unit == "C" else f2c(lo - 0.5))
    b = math.inf if hi is None else (hi + 0.5 if unit == "C" else f2c(hi + 0.5))
    return a, b


def fetch_events():
    out, off = [], 0
    while True:
        q = urllib.parse.urlencode({"tag_id": CFG["gamma_tag_id"], "closed": "false", "limit": 100, "offset": off})
        page = http(f"{GAMMA}/events?{q}") or []
        out += page
        if len(page) < 100:
            break
        off += 100
    evs = []
    for e in out:
        title = e.get("title", "")
        if not title.startswith(CFG["title_prefix"]):
            continue
        city = re.sub(r"^Highest temperature in (.*?) on .*$", r"\1", title)
        m = re.search(r"\(([A-Z0-9]{4})\)", e.get("description", ""))
        mk = [x for x in e.get("markets", []) if x.get("clobTokenIds") and x.get("groupItemTitle")]
        evs.append({"id": str(e["id"]), "slug": e.get("slug"), "title": title, "city": city,
                    "date": e.get("eventDate") or (e.get("endDate") or "")[:10],
                    "icao": m.group(1) if m else None, "markets": mk})
    return evs


def fetch_stations(icaos, cache):
    need = [i for i in icaos if i not in cache]
    for k in range(0, len(need), 40):
        ids = ",".join(need[k:k + 40])
        try:
            for s in http(f"{AWC}/stationinfo?ids={ids}&format=json") or []:
                cache[s["icaoId"]] = {"lat": s["lat"], "lon": s["lon"], "name": s.get("site")}
        except Exception as e:
            log(f"stationinfo error {ids}: {e}")
    return cache


def fetch_metar_max(icaos, hours=96):
    """{icao: [(utc_datetime, temp_c)]} routine METARs only (SPECI excluded).
    One station per request: the API caps a response at 400 rows."""
    from concurrent.futures import ThreadPoolExecutor

    def one(icao):
        try:
            rows = http(f"{AWC}/metar?ids={icao}&format=json&hours={hours}") or []
        except Exception as e:
            log(f"metar error {icao}: {e}")
            return icao, []
        out = []
        for r in rows:
            raw = r.get("rawOb") or ""
            if r.get("metarType") == "SPECI" or raw.startswith("SPECI") or r.get("temp") is None:
                continue
            out.append((datetime.fromtimestamp(r["obsTime"], timezone.utc), float(r["temp"])))
        return icao, out
    with ThreadPoolExecutor(8) as ex:
        return {i: o for i, o in ex.map(one, icaos) if o}


def fetch_ensembles(stations):
    """{icao: {"tz": name, "times": [...local naive dt], "members": {model: [[hourly]...]}}}"""
    res, icaos = {}, list(stations)
    n = CFG["openmeteo_chunk"]
    for k in range(0, len(icaos), n):
        chunk = icaos[k:k + n]
        q = urllib.parse.urlencode({
            "latitude": ",".join(str(stations[i]["lat"]) for i in chunk),
            "longitude": ",".join(str(stations[i]["lon"]) for i in chunk),
            "hourly": "temperature_2m", "models": ",".join(CFG["ensemble_models"]),
            "timezone": "auto", "past_days": CFG["past_days_for_bias"], "forecast_days": 4})
        try:
            d = http(f"{ENS}?{q}", timeout=120)
        except Exception as e:
            log(f"open-meteo error, skipped {','.join(chunk)}: {e}")
            continue
        if isinstance(d, dict):
            d = [d]
        for icao, loc in zip(chunk, d):
            h = loc["hourly"]
            members = {}
            for key, vals in h.items():
                if key == "time":
                    continue
                model = next((m for m in ("ncep_gefs", "ecmwf_ifs", "icon") if m in key), key)
                members.setdefault(model, []).append(vals)
            res[icao] = {"tz": loc["timezone"], "times": [datetime.fromisoformat(t) for t in h["time"]], "members": members}
    return res


# ---------------------------------------------------------------- model
def phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def daily_max(vals, times, day, after=None):
    xs = [v for v, t in zip(vals, times) if t.date() == day and v is not None and (after is None or t >= after)]
    return max(xs) if xs else None


def station_bias(ens, obs, tz):
    """Shrunk mean of (observed METAR daily max - ensemble-mean daily max) over recent full days."""
    if not obs:
        return 0.0, 0
    local_today = NOW.astimezone(tz).date()
    diffs = []
    for back in range(1, CFG["past_days_for_bias"] + 1):
        day = local_today - timedelta(days=back)
        o = [t for dt, t in obs if dt.astimezone(tz).date() == day]
        if len(o) < 12:
            continue
        fc = []
        for runs in ens["members"].values():
            ms = [daily_max(v, ens["times"], day) for v in runs]
            ms = [m for m in ms if m is not None]
            if ms:
                fc.append(sum(ms) / len(ms))
        if fc:
            diffs.append(max(o) - sum(fc) / len(fc))
    if not diffs:
        return 0.0, 0
    b = CFG["bias_shrink"] * sum(diffs) / len(diffs)
    return max(-CFG["bias_clip_c"], min(CFG["bias_clip_c"], b)), len(diffs)


def model_probs(ens, obs, tz, day, bounds, bias):
    """Bucket probabilities. Members (equal weight per model) are spread-inflated, bias-shifted and
    smoothed with a Gaussian kernel; for today the observed max so far is a hard floor."""
    now_local = NOW.astimezone(tz).replace(tzinfo=None)
    today = now_local.date()
    after, floor = None, -math.inf
    if day == today:
        after = now_local.replace(minute=0, second=0, microsecond=0)
        today_obs = [(dt.astimezone(tz).replace(tzinfo=None), t) for dt, t in (obs or []) if dt.astimezone(tz).date() == day]
        if today_obs:
            floor = max(t for _, t in today_obs)
            # nowcast: replace station bias by (latest obs - ensemble mean at that hour), if obs is recent
            lt, lv = max(today_obs)
            if (now_local - lt).total_seconds() < 3 * 3600:
                hr = lt.replace(minute=0, second=0, microsecond=0) + (timedelta(hours=1) if lt.minute >= 30 else timedelta())
                if hr in ens["times"]:
                    j = ens["times"].index(hr)
                    vals = [run[j] for runs in ens["members"].values() for run in runs if run[j] is not None]
                    if vals:
                        nb = max(-CFG["nowcast_clip_c"], min(CFG["nowcast_clip_c"], lv - sum(vals) / len(vals)))
                        w = CFG["nowcast_weight"]
                        bias = w * nb + (1 - w) * bias
    lead = max(0.0, (datetime.combine(day, datetime.min.time()).replace(hour=15) - now_local).total_seconds() / 86400)
    sd = CFG["kernel_sd_base_c"] + CFG["kernel_sd_per_lead_day_c"] * lead
    probs = [0.0] * len(bounds)
    used = 0
    for runs in ens["members"].values():
        ms = [daily_max(v, ens["times"], day, after) for v in runs]
        ms = [m for m in ms if m is not None]
        if not ms:
            continue
        used += 1
        mean = sum(ms) / len(ms)
        for m in ms:
            mu = mean + CFG["spread_inflation"] * (m - mean) + bias
            for i, (a, b) in enumerate(bounds):
                fa = 0.0 if a <= floor else phi((a - mu) / sd)
                fb = 0.0 if b <= floor else phi((b - mu) / sd)
                probs[i] += (fb - fa) / len(ms)  # each model weighted equally
    if used == 0:
        return None, bias
    probs = [p / used for p in probs]
    s = sum(probs)
    return ([p / s for p in probs] if s > 0 else None), bias


# ---------------------------------------------------------------- market
def fetch_books(token_ids):
    books = {}
    for k in range(0, len(token_ids), 100):
        try:
            for b in http(f"{CLOB}/books", body=[{"token_id": t} for t in token_ids[k:k + 100]]) or []:
                books[b["asset_id"]] = b
        except Exception as e:
            log(f"clob books error: {e}")
    return books


def book_side(book):
    if not book:
        return None, None, []
    bids = sorted(((float(x["price"]), float(x["size"])) for x in book.get("bids", [])), reverse=True)
    asks = sorted((float(x["price"]), float(x["size"])) for x in book.get("asks", []))
    return (bids[0][0] if bids else None), (asks[0][0] if asks else None), asks


def fee_per_share(p, rate):
    return rate * p * (1 - p)


def simulate_fill(asks, stake, rate):
    """Walk the ask book to spend `stake` USD. Returns (shares, avg_price, fee_usd) or None."""
    left, shares, fee = stake, 0.0, 0.0
    for p, sz in asks:
        unit = p + fee_per_share(p, rate)
        take = min(sz, left / unit)
        shares += take
        fee += take * fee_per_share(p, rate)
        left -= take * unit
        if left <= 1e-9:
            break
    if left > 1e-6 or shares <= 0:
        return None
    return shares, (stake - fee) / shares, fee


# ---------------------------------------------------------------- storage
TRADE_COLS = ["trade_id", "opened_utc", "event_id", "city", "date", "icao", "bucket", "side", "token_id",
              "model_p", "ask", "avg_price", "shares", "stake_usd", "fee_usd", "edge", "status", "payout_usd", "pnl_usd", "settled_utc"]


def load_json(name, default):
    p = os.path.join(DATA, name)
    return json.load(open(p)) if os.path.exists(p) else default


def save_json(name, obj):
    with open(os.path.join(DATA, name), "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)


def load_trades():
    p = os.path.join(DATA, "trades.csv")
    return list(csv.DictReader(open(p))) if os.path.exists(p) else []


def save_trades(rows):
    with open(os.path.join(DATA, "trades.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_COLS)
        w.writeheader()
        w.writerows(rows)


def r3(x):
    return "" if x is None else round(x, 4)


# ---------------------------------------------------------------- settlement & scoring
def winner_index(markets):
    if not all(m.get("closed") for m in markets):
        return None
    wins = [i for i, m in enumerate(markets) if json.loads(m.get("outcomePrices") or "[0,0]")[0] in ("1", 1, "1.0")]
    return wins[0] if len(wins) == 1 else None


def settle(state, trades):
    n = 0
    for eid, ev in state["events"].items():
        if ev.get("winner") is not None or ev["date"] >= NOW.date().isoformat():
            continue
        try:
            e = http(f"{GAMMA}/events/{eid}")
        except Exception as ex:
            log(f"settle fetch error {eid}: {ex}")
            continue
        mk = [x for x in e.get("markets", []) if x.get("groupItemTitle")]
        labels = {m["groupItemTitle"]: i for i, m in enumerate(mk)}
        w = winner_index(mk)
        if w is None:
            continue
        win_label = mk[w]["groupItemTitle"]
        ev["winner"] = ev["buckets"].index(win_label) if win_label in ev["buckets"] else None
        ev["winner_label"] = win_label
        n += 1
        for t in trades:
            if t["event_id"] == eid and t["status"] == "open":
                yes_win = t["bucket"] == win_label
                won = yes_win if t["side"] == "YES" else not yes_win
                payout = float(t["shares"]) if won else 0.0
                t.update(status="won" if won else "lost", payout_usd=r3(payout),
                         pnl_usd=r3(payout - float(t["stake_usd"])), settled_utc=NOW.isoformat(timespec="seconds"))
    return n


def score(p, w):
    brier = sum((pi - (1 if i == w else 0)) ** 2 for i, pi in enumerate(p))
    ll = -math.log(max(p[w], 1e-3))
    return brier, ll


# ---------------------------------------------------------------- report
def build_report(state, trades, run):
    settled = [t for t in trades if t["status"] in ("won", "lost")]
    pnl = sum(float(t["pnl_usd"]) for t in settled)
    staked = sum(float(t["stake_usd"]) for t in settled)
    wins = sum(t["status"] == "won" for t in settled)
    agg = {"last": {"n": 0, "mb": 0, "kb": 0, "ml": 0, "kl": 0}, "first": {"n": 0, "mb": 0, "kb": 0, "ml": 0, "kl": 0}}
    cities = {}
    for ev in state["events"].values():
        w = ev.get("winner")
        c = cities.setdefault(ev["city"], {"ev": 0, "mb": 0, "kb": 0, "trades": 0, "pnl": 0.0, "open": 0})
        if w is None:
            continue
        for which in ("last", "first"):
            s = ev.get(which)
            if not s or not s.get("market"):
                continue
            mb, ml = score(s["model"], w)
            kb, kl = score(s["market"], w)
            a = agg[which]
            a["n"] += 1; a["mb"] += mb; a["kb"] += kb; a["ml"] += ml; a["kl"] += kl
            if which == "last":
                c["ev"] += 1; c["mb"] += mb; c["kb"] += kb
    for t in trades:
        c = cities.setdefault(t["city"], {"ev": 0, "mb": 0, "kb": 0, "trades": 0, "pnl": 0.0, "open": 0})
        c["trades"] += 1
        if t["status"] == "open":
            c["open"] += 1
        elif t["pnl_usd"] != "":
            c["pnl"] += float(t["pnl_usd"])

    def avg(a, k):
        return round(a[k] / a["n"], 4) if a["n"] else None
    summary = {
        "updated_utc": NOW.isoformat(timespec="seconds"),
        "trades_total": len(trades), "trades_open": sum(t["status"] == "open" for t in trades),
        "trades_settled": len(settled), "wins": wins,
        "win_rate": round(wins / len(settled), 4) if settled else None,
        "staked_settled_usd": round(staked, 2), "pnl_usd": round(pnl, 2),
        "roi": round(pnl / staked, 4) if staked else None,
        "open_stake_usd": round(sum(float(t["stake_usd"]) for t in trades if t["status"] == "open"), 2),
        "scoring": {w: {"events": agg[w]["n"], "model_brier": avg(agg[w], "mb"), "market_brier": avg(agg[w], "kb"),
                        "model_logloss": avg(agg[w], "ml"), "market_logloss": avg(agg[w], "kl")} for w in agg},
        "last_run": run,
    }
    save_json("summary.json", summary)

    f = lambda x, d=3: "—" if x is None else (f"{x:.{d}f}" if isinstance(x, float) else str(x))
    L = ["# Polymarket 最高温 纸面交易报告", "",
         f"> 自动生成，最后更新：{NOW.strftime('%Y-%m-%d %H:%M')} UTC（北京时间 {NOW.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M')}）。",
         "> **纸面交易，没有真钱、没有钱包、没有真实下单。**", "",
         "## 总览", "",
         "| 项目 | 数值 |", "|---|---|",
         f"| 模拟交易总数 | {summary['trades_total']} |",
         f"| 未结算 / 已结算 | {summary['trades_open']} / {summary['trades_settled']} |",
         f"| 未结算占用资金 | ${summary['open_stake_usd']:.2f} |",
         f"| 已结算投入 | ${summary['staked_settled_usd']:.2f} |",
         f"| 已结算盈亏（含手续费） | ${summary['pnl_usd']:+.2f} |",
         f"| 收益率 | {f(summary['roi'] * 100 if summary['roi'] is not None else None, 1)}{'%' if summary['roi'] is not None else ''} |",
         f"| 胜率 | {f(summary['win_rate'] * 100 if summary['win_rate'] is not None else None, 1)}{'%' if summary['win_rate'] is not None else ''} |", "",
         "## 模型 vs 市场（已结算事件）", "",
         "数值越小越好。“最后一次”= 结算前最后一次扫描时的概率；“第一次”= 第一次扫到这个事件时（通常提前 1–2 天）。", "",
         "| 时点 | 事件数 | 模型 Brier | 市场 Brier | 模型 对数损失 | 市场 对数损失 |", "|---|---|---|---|---|---|"]
    for w, name in (("first", "第一次"), ("last", "最后一次")):
        s = summary["scoring"][w]
        L.append(f"| {name} | {s['events']} | {f(s['model_brier'])} | {f(s['market_brier'])} | {f(s['model_logloss'])} | {f(s['market_logloss'])} |")
    L += ["", "如果模型的分数长期比市场差，说明模型没有优势，纸面盈利也多半是运气。", "",
          "## 分城市", "", "| 城市 | 已结算事件 | 模型 Brier | 市场 Brier | 模拟交易 | 未结算 | 已结算盈亏 |", "|---|---|---|---|---|---|---|"]
    for name in sorted(cities):
        c = cities[name]
        mb = c["mb"] / c["ev"] if c["ev"] else None
        kb = c["kb"] / c["ev"] if c["ev"] else None
        L.append(f"| {name} | {c['ev']} | {f(mb)} | {f(kb)} | {c['trades']} | {c['open']} | ${c['pnl']:+.2f} |")
    L += ["", "## 最近一次运行", "",
          f"- 扫描事件：{run['events_scanned']} 个，城市 {run['cities']} 个；有模型概率的事件 {run['events_modeled']} 个",
          f"- 本次新增模拟交易：{run['new_trades']} 笔；本次结算事件：{run['settled_events']} 个",
          f"- 跳过：{'; '.join(run['skipped']) if run['skipped'] else '无'}", ""]
    recent = trades[-15:][::-1]
    if recent:
        L += ["## 最近 15 笔模拟交易", "", "| 开仓(UTC) | 城市 | 日期 | 档位 | 方向 | 模型概率 | 均价 | 金额 | 状态 | 盈亏 |", "|---|---|---|---|---|---|---|---|---|---|"]
        st = {"open": "未结算", "won": "赢", "lost": "输"}
        for t in recent:
            L.append(f"| {t['opened_utc'][:16]} | {t['city']} | {t['date']} | {t['bucket']} | {t['side']} | {float(t['model_p']):.2f} | {float(t['avg_price']):.3f} | ${float(t['stake_usd']):.2f} | {st.get(t['status'], t['status'])} | {t['pnl_usd'] or '—'} |")
    open(os.path.join(ROOT, "REPORT.md"), "w").write("\n".join(L) + "\n")


# ---------------------------------------------------------------- main
def main():
    os.makedirs(DATA, exist_ok=True)
    state = load_json("state.json", {"events": {}})
    stations = load_json("stations.json", {})
    trades = load_trades()
    skipped = []

    settled_n = settle(state, trades)

    events = fetch_events()
    log(f"open highest-temperature events: {len(events)}")
    keep = []
    for e in events:
        if e["city"] in CFG["skip_cities"]:
            skipped.append(f"{e['city']} {e['date']}: 配置跳过（结算源非 METAR）")
        elif not e["icao"]:
            skipped.append(f"{e['city']} {e['date']}: 描述里没找到 ICAO 机场代码")
        else:
            keep.append(e)
    icaos = sorted({e["icao"] for e in keep})
    stations = fetch_stations(icaos, stations)
    save_json("stations.json", stations)
    have = {i: stations[i] for i in icaos if i in stations}
    obs = fetch_metar_max(list(have))
    ens = fetch_ensembles(have)

    tokens = []
    for e in keep:
        for m in e["markets"]:
            tokens += json.loads(m["clobTokenIds"])
    books = fetch_books(tokens)

    today_utc = NOW.date().isoformat()
    spent_today = sum(float(t["stake_usd"]) for t in trades if t["opened_utc"][:10] == today_utc)
    held = {(t["event_id"], t["bucket"]) for t in trades}
    snap_rows, candidates, modeled = [], [], 0
    bias_cache = {}
    for e in keep:
        icao = e["icao"]
        if icao not in ens:
            skipped.append(f"{e['city']} {e['date']}: 没拿到集合预报（{'站点坐标缺失' if icao not in have else 'Open-Meteo 出错/额度'}）")
            continue
        tz = ZoneInfo(ens[icao]["tz"])
        day = datetime.fromisoformat(e["date"]).date()
        local_now = NOW.astimezone(tz)
        if day < local_now.date():
            continue  # local day over; waiting for resolution
        labels = [m["groupItemTitle"] for m in e["markets"]]
        parsed = [parse_bucket(x) for x in labels]
        bounds = [bucket_bounds_c(*p) for p in parsed]
        if icao not in bias_cache:
            bias_cache[icao] = station_bias(ens[icao], obs.get(icao), tz)
        bias, nb = bias_cache[icao]
        probs, bias = model_probs(ens[icao], obs.get(icao), tz, day, bounds, bias)
        if probs is None:
            skipped.append(f"{e['city']} {e['date']}: 预报不覆盖该日期")
            continue
        modeled += 1
        mids = []
        for m, p, lab in zip(e["markets"], probs, labels):
            ty, tn = json.loads(m["clobTokenIds"])[:2]
            yb, ya, yasks = book_side(books.get(ty))
            nbid, na, nasks = book_side(books.get(tn))
            gp = float(json.loads(m.get("outcomePrices") or '["0"]')[0] or 0)
            mid = (yb + ya) / 2 if yb is not None and ya is not None and ya - yb <= 0.1 else gp
            mids.append(mid)
            snap_rows.append([NOW.isoformat(timespec="seconds"), e["id"], e["city"], e["date"], icao, lab,
                              r3(p), r3(yb), r3(ya), r3(na), r3(mid), r3(bias)])
            if (e["id"], lab) in held or not m.get("acceptingOrders", True):
                continue
            rate = float((m.get("feeSchedule") or {}).get("rate", CFG["fee_rate"])) if m.get("feesEnabled", True) else 0.0
            for side, mp, ask, asks, tok in (("YES", p, ya, yasks, ty), ("NO", 1 - p, na, nasks, tn)):
                if ask is None or not (CFG["min_ask"] <= ask <= CFG["max_ask"]):
                    continue
                if mp - (ask + fee_per_share(ask, rate)) < CFG["min_edge"]:
                    continue
                fill = simulate_fill(asks, CFG["stake_usd"], rate)
                if not fill:
                    continue
                shares, avgp, fee = fill
                edge = mp - (avgp + fee / shares)
                if edge >= CFG["min_edge"]:
                    candidates.append((edge, e, lab, side, tok, mp, ask, avgp, shares, fee))
        s = sum(mids)
        market = [x / s for x in mids] if s > 0 else None
        ev = state["events"].setdefault(e["id"], {"city": e["city"], "date": e["date"], "icao": icao, "buckets": labels, "slug": e["slug"]})
        ev["buckets"] = labels
        snap = {"ts": NOW.isoformat(timespec="seconds"), "model": [round(x, 5) for x in probs],
                "market": [round(x, 5) for x in market] if market else None}
        ev.setdefault("first", snap)
        ev["last"] = snap

    candidates.sort(key=lambda c: -c[0])
    new = 0
    for edge, e, lab, side, tok, mp, ask, avgp, shares, fee in candidates:
        if (e["id"], lab) in held or spent_today + CFG["stake_usd"] > CFG["max_new_stake_per_day_usd"] + 1e-9:
            continue
        held.add((e["id"], lab))
        spent_today += CFG["stake_usd"]
        new += 1
        trades.append({"trade_id": f"{NOW.strftime('%Y%m%d%H%M')}-{e['id']}-{len(trades)}", "opened_utc": NOW.isoformat(timespec="seconds"),
                       "event_id": e["id"], "city": e["city"], "date": e["date"], "icao": e["icao"], "bucket": lab, "side": side,
                       "token_id": tok, "model_p": r3(mp), "ask": r3(ask), "avg_price": r3(avgp), "shares": r3(shares),
                       "stake_usd": CFG["stake_usd"], "fee_usd": r3(fee), "edge": r3(edge), "status": "open", "payout_usd": "", "pnl_usd": "", "settled_utc": ""})
        log(f"PAPER TRADE {side} {e['city']} {e['date']} [{lab}] model={mp:.2f} ask={ask:.3f} edge={edge:.3f}")

    if snap_rows:
        d = os.path.join(DATA, "snapshots", NOW.strftime("%Y-%m-%d"))
        os.makedirs(d, exist_ok=True)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["ts_utc", "event_id", "city", "date", "icao", "bucket", "model_p", "yes_bid", "yes_ask", "no_ask", "mid", "bias_c"])
        w.writerows(snap_rows)
        with gzip.open(os.path.join(d, NOW.strftime("%H%M%S") + ".csv.gz"), "wt") as g:
            g.write(buf.getvalue())

    # forget fully finished events older than 30 days with no open trades (keeps state small)
    cutoff = (NOW - timedelta(days=30)).date().isoformat()
    state["events"] = {k: v for k, v in state["events"].items() if not (v["date"] < cutoff and v.get("winner") is not None)}

    save_trades(trades)
    save_json("state.json", state)
    run = {"ts_utc": NOW.isoformat(timespec="seconds"), "events_scanned": len(events),
           "cities": len({e["city"] for e in events}), "events_modeled": modeled,
           "new_trades": new, "settled_events": settled_n, "snapshot_rows": len(snap_rows),
           "skipped": sorted(set(skipped)), "errors": [x for x in LOG if "error" in x]}
    build_report(state, trades, run)
    log(json.dumps({k: v for k, v in run.items() if k != "skipped"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
