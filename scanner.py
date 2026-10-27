"""Сканер OKX: звезда на 1ч/4ч + начало падения на 15м/30м -> сообщение в Telegram.

Схема по свечам (на старшем таймфрейме):
  свеча 1 - звезда;
  свеча 2 - основное движение: бот следит за ней по свечам 15м/30м и пишет,
            как только одна из них закрылась ниже минимума звезды;
  свеча 3 - остатки движения: сообщений уже не будет.

Порядок работы одного запуска (из-за паузы между запросами поиск новых звёзд долгий):
  1) сначала проверка уже найденных звёзд (срочно),
  2) потом поиск новых звёзд на 1ч/4ч (долго),
  3) новые звёзды сразу проверяются ещё раз - на случай, если запуск был с большой задержкой.
Свечи 30м строятся из двух свечей 15м, отдельный запрос не нужен.
"""
import datetime as dt
import json
import os
import sys
import time

import requests

import config as C

OKX = "https://www.okx.com"
TASHKENT = dt.timezone(dt.timedelta(hours=5))
TF_MS = {"15m": 900_000, "30m": 1_800_000, "1H": 3_600_000, "4H": 14_400_000}
TF_RU = {"15m": "15м", "30m": "30м", "1H": "1ч", "4H": "4ч"}
TV_INTERVAL = {"15m": "15", "30m": "30", "1H": "60", "4H": "240"}
REFERENCE = "BTC-USDT-SWAP"
STATE_FILE = os.environ.get("STATE_FILE", "state/state.json")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
PAUSE = float(os.environ.get("REQUEST_PAUSE", C.REQUEST_PAUSE))
BUDGET_S = float(os.environ.get("RUN_BUDGET_MIN", "20")) * 60  # дольше этого новые монеты не начинаем
MAX_CONSEC_FAIL = 5     # столько ошибок подряд - биржа недоступна, поиск прерываем
CANDLES = 100           # свечей на запрос (для RSI и среднего объёма)
RSI_LEN = 14
DAY_MS = 86_400_000
T0 = time.time()


def log(msg):
    print(msg, flush=True)


def now_ms():
    return int(time.time() * 1000)


def fmt_time(ts_ms):
    return dt.datetime.fromtimestamp(ts_ms / 1000, TASHKENT).strftime("%d.%m %H:%M")


def fmt_hm(ts_ms):
    return dt.datetime.fromtimestamp(ts_ms / 1000, TASHKENT).strftime("%H:%M")


def price(x):
    s = f"{x:.2f}" if x >= 1000 else f"{x:.4f}" if x >= 1 else f"{x:.8f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def mean(xs):
    return sum(xs) / len(xs)


# ---------------------------------------------------------------- Индикаторы

def rsi(closes, n=RSI_LEN):
    """RSI по Уайлдеру (как в TradingView). None, если свечей мало."""
    if len(closes) < n + 1:
        return None
    gain = loss = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gain += max(d, 0.0)
        loss += max(-d, 0.0)
    gain, loss = gain / n, loss / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        gain = (gain * (n - 1) + max(d, 0.0)) / n
        loss = (loss * (n - 1) + max(-d, 0.0)) / n
    if loss == 0:
        return 50.0 if gain == 0 else 100.0
    return 100.0 - 100.0 / (1.0 + gain / loss)


def rsi_text(v):
    zone = "перекупленность" if v >= 70 else "перепроданность" if v <= 30 else "нейтрально"
    return f"{v:.0f} ({zone})"


def to_30m(cs15):
    """Свечи 30м из свечей 15м: пара свечей с началом в :00 и :30."""
    half, full = TF_MS["15m"], TF_MS["30m"]
    groups = {}
    for c in cs15:
        groups.setdefault(c["ts"] // full, {})[(c["ts"] % full) // half] = c
    out = []
    for b in sorted(groups):
        g = groups[b]
        if 0 in g and 1 in g:
            a, z = g[0], g[1]
            out.append({"ts": b * full, "o": a["o"], "h": max(a["h"], z["h"]), "l": min(a["l"], z["l"]),
                        "c": z["c"], "v": a["v"] + z["v"], "closed": a["closed"] and z["closed"]})
    return out


def vol_mult_at(series, ts):
    """Объём закрытой свечи с началом ts относительно среднего за 20 предыдущих свечей."""
    closed = [x for x in series if x["closed"]]
    for i, x in enumerate(closed):
        if x["ts"] == ts:
            if i < 20:
                return None
            avg = mean([y["v"] for y in closed[i - 20:i]])
            return x["v"] / avg if avg > 0 else None
    return None


# ---------------------------------------------------------------- OKX

def okx_get(path, params, tries=3):
    """GET к публичному API OKX с повтором и нарастающей паузой (не дольше 30 секунд)."""
    for attempt in range(tries):
        try:
            r = requests.get(OKX + path, params=params, timeout=15)
            if r.status_code == 200:
                j = r.json()
                if j.get("code") == "0":
                    return j.get("data", [])
                log(f"  OKX отказал: code={j.get('code')} {j.get('msg')} {params}")
                if j.get("code") == "51001":  # такого инструмента нет, повторять нет смысла
                    return None
            else:
                log(f"  HTTP {r.status_code}: {path} {params}")
        except (requests.RequestException, ValueError) as e:
            log(f"  Ошибка запроса: {e}")
        if attempt < tries - 1:
            time.sleep(min(max(PAUSE, 0.5) * (2 ** attempt), 30))
    return None


def fetch_candles(inst, bar, limit):
    """Свечи от старых к новым. closed=False у ещё не закрытой свечи."""
    data = okx_get("/api/v5/market/candles", {"instId": inst, "bar": bar, "limit": str(limit)})
    time.sleep(PAUSE)
    if not data:
        return None
    out = []
    for r in reversed(data):
        out.append({"ts": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
                    "c": float(r[4]), "v": float(r[5]), "closed": r[8] == "1"})
    return out


def top_instruments():
    """Самые оборотистые USDT-перпетуалы OKX."""
    data = okx_get("/api/v5/market/tickers", {"instType": "SWAP"})
    time.sleep(PAUSE)
    if not data:
        return None
    rows = []
    for d in data:
        inst = d.get("instId", "")
        if not inst.endswith("-USDT-SWAP") or inst in C.EXCLUDE:
            continue
        try:
            turnover = float(d["volCcy24h"]) * float(d["last"])
        except (KeyError, ValueError, TypeError):
            continue
        rows.append((turnover, inst))
    rows.sort(reverse=True)
    return [inst for _, inst in rows[:C.TOP_COINS]]


# ---------------------------------------------------------------- Telegram

def send(text):
    if DRY_RUN:
        log("----- [DRY_RUN] сообщение не отправлено, вот его текст -----\n" + text + "\n-----")
        return True
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log("  Нет секретов TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (Settings -> Secrets and variables -> Actions)")
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                                "disable_web_page_preview": True}, timeout=15)
    except requests.RequestException as e:
        log(f"  Telegram: ошибка запроса: {e}")
        return False
    if r.status_code != 200:
        log(f"  Telegram: HTTP {r.status_code}: {r.text[:200]}")
        return False
    return True


# ---------------------------------------------------------------- Звезда

def star_info(cs, i):
    """Проверка свечи cs[i] (закрытой). cs - только закрытые свечи, от старых к новым."""
    c = cs[i]
    rng = c["h"] - c["l"]
    if rng <= 0:
        return None
    top, bottom = max(c["o"], c["c"]), min(c["o"], c["c"])
    body, upper, lower = top - bottom, c["h"] - top, bottom - c["l"]
    if not (upper / rng > C.UPPER_WICK_MIN and body / rng < C.BODY_MAX and lower / rng < C.LOWER_WICK_MAX):
        return None
    if C.REQUIRE_RED_BODY and not c["c"] < c["o"]:
        return None
    lb = C.LOCAL_HIGH_LOOKBACK
    if i < max(lb, 21):
        return None
    if c["h"] < max(x["h"] for x in cs[i - lb:i]):  # звезда - локальный максимум
        return None
    avg_vol = mean([x["v"] for x in cs[i - 20:i]])
    vol_mult = c["v"] / avg_vol if avg_vol > 0 else None
    if C.MIN_VOLUME_MULT > 0 and (vol_mult is None or vol_mult < C.MIN_VOLUME_MULT):
        return None
    extra = {}
    if C.PATTERN == "evening_star":
        p = cs[i - 1]
        p_body = p["c"] - p["o"]
        avg_body = mean([abs(x["c"] - x["o"]) for x in cs[i - 21:i - 1]])
        if p_body <= 0 or p_body < avg_body * getattr(C, "PREV_GREEN_BODY_MULT", 1.0):
            return None
        if body > p_body * getattr(C, "STAR_BODY_VS_PREV_MAX", 0.7):
            return None
        extra = {"prev_mult": p_body / avg_body if avg_body > 0 else None,
                 "star_vs_prev": body / p_body * 100}
    return {"upper_pct": upper / rng * 100, "body_pct": body / rng * 100, "vol_mult": vol_mult,
            "rsi_star": rsi([x["c"] for x in cs[:i + 1]]), **extra}


def level_of(w):
    return w["l"] if C.CONFIRM_LEVEL == "low" else min(w["o"], w["c"])


def breakdown(c, w):
    """Свеча младшего ТФ закрылась ниже уровня звезды (и красная, если так задано)."""
    if not c["c"] < level_of(w):
        return False
    if C.REQUIRE_RED_CONFIRM and not c["c"] < c["o"]:
        return False
    return True


# ---------------------------------------------------------------- Сообщение

def build_message(w, c, bar, close_ts, ctx):
    """ctx: cur - цена сейчас, now - время запроса, rsi_now, vol_break."""
    base = w["inst"].split("-")[0]
    level = level_of(w)
    level_name = "минимума звезды" if C.CONFIRM_LEVEL == "low" else "низа тела звезды"
    url = f"https://www.tradingview.com/chart/?symbol=OKX:{base}USDT.P&interval={TV_INTERVAL[bar]}"
    star_tf = TF_RU[w["tf"]]
    cur = ctx["cur"]

    lines = [
        f"{C.EMOJI} <b>{C.TITLE}</b> · {base}/USDT",
        "",
        f"Цена сейчас: {price(cur)} ({fmt_hm(ctx['now'])}, Ташкент)",
        f"Звезда: {star_tf}, закрылась {fmt_time(w['ts'] + TF_MS[w['tf']])}",
    ]
    if w.get("prev_mult") is not None:
        lines.append(f"Перед звездой: зелёная свеча {star_tf}, тело ×{w['prev_mult']:.1f} от среднего, "
                     f"тело звезды {w['star_vs_prev']:.0f}% от неё")

    rsi_parts = []
    if w.get("rsi_star") is not None:
        rsi_parts.append(f"на звезде {star_tf} — {rsi_text(w['rsi_star'])}")
    if ctx.get("rsi_now") is not None:
        rsi_parts.append(f"сейчас на 15м — {rsi_text(ctx['rsi_now'])}")
    if rsi_parts:
        lines.append("RSI(14): " + ", ".join(rsi_parts))

    vol_parts = []
    if w.get("vol_mult"):
        vol_parts.append(f"звезда ×{w['vol_mult']:.1f} от среднего")
    if ctx.get("vol_break"):
        vol_parts.append(f"свеча пробоя ×{ctx['vol_break']:.1f}")
    if vol_parts:
        lines.append("Объём: " + ", ".join(vol_parts))

    below = (level - c["c"]) / level * 100
    to_cancel = (w["h"] - cur) / cur * 100
    lines += [
        "",
        f"Падение началось: свеча {TF_RU[bar]} закрылась в {fmt_hm(close_ts)} на {price(c['c'])},",
        f"ниже {level_name} ({price(level)}) на {below:.2f}%",
        "",
        f"Отмена сценария: закрытие выше {price(w['h'])} ({to_cancel:+.1f}% от цены сейчас)",
        "",
        f'<a href="{url}">График TradingView</a>',
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- Шаги работы

def scan_new_stars(state, now, stats, new_keys):
    insts = None
    for tf in C.STAR_TIMEFRAMES:
        if time.time() - T0 > BUDGET_S:
            log(f"[{tf}] лимит времени запуска исчерпан, поиск продолжится в следующем запуске")
            return
        tf_ms = TF_MS[tf]
        if state["scanned"].get(tf) == (now // tf_ms) * tf_ms - tf_ms:
            continue  # свеча этого ТФ уже проверена, лишний запрос не нужен
        ref = fetch_candles(REFERENCE, tf, 3)
        ref_closed = [c for c in ref or [] if c["closed"]]
        if not ref_closed:
            log(f"[{tf}] не удалось получить опорные свечи, пропускаю")
            stats["errors"] += 1
            continue
        ref_ts = ref_closed[-1]["ts"]
        if state["scanned"].get(tf) == ref_ts:
            continue  # новая свеча этого ТФ ещё не закрылась
        if insts is None:
            insts = top_instruments()
            if not insts:
                log("Не удалось получить список монет")
                stats["errors"] += 1
                return
        log(f"[{tf}] закрылась свеча {fmt_time(ref_ts)}: проверяю {len(insts)} монет")
        done = failed = found = consec = 0
        complete = True
        for inst in insts:
            if time.time() - T0 > BUDGET_S:
                log(f"[{tf}] лимит времени запуска исчерпан после {done} монет")
                complete = False
                break
            done += 1
            cs = fetch_candles(inst, tf, CANDLES)
            if not cs:
                failed += 1
                consec += 1
                if consec >= MAX_CONSEC_FAIL:
                    log(f"[{tf}] {consec} ошибок подряд, биржа недоступна, прерываю поиск")
                    complete = False
                    break
                continue
            consec = 0
            closed = [c for c in cs if c["closed"]]
            if len(closed) < 25 or closed[-1]["ts"] != ref_ts:
                continue
            info = star_info(closed, len(closed) - 1)
            if not info:
                continue
            key = f"{inst}|{tf}|{ref_ts}"
            if key in state["seen"]:
                continue
            c = closed[-1]
            state["seen"][key] = ref_ts
            state["watch"][key] = {"inst": inst, "tf": tf, "ts": ref_ts, "o": c["o"], "h": c["h"],
                                   "l": c["l"], "c": c["c"], **info}
            new_keys.add(key)
            found += 1
            log(f"  звезда: {inst} {tf}")
        stats["scanned"][tf] = done - failed
        stats["failed"] += failed
        stats["found"] += found
        if complete and failed <= max(3, len(insts) // 10):
            state["scanned"][tf] = ref_ts
        else:
            log(f"[{tf}] проверка неполная (ошибок {failed}), повторю в следующем запуске")
        return  # за один запуск ищем звёзды только на одном таймфрейме, следующий - в следующем запуске


def check_watch(state, now, stats, only=None):
    first_close = min(TF_MS[b] for b in C.CONFIRM_TIMEFRAMES)
    for key, w in list(state["watch"].items()):
        if only is not None and key not in only:
            continue
        tf_ms = TF_MS[w["tf"]]
        start, end = w["ts"] + tf_ms, w["ts"] + 2 * tf_ms  # окно = свеча 2
        if now < start + first_close:
            continue  # в свече 2 ещё не закрылась ни одна свеча 15м/30м, запрос не нужен

        cs = fetch_candles(w["inst"], "15m", CANDLES)
        events, series = [], {}
        if cs is None:
            stats["failed"] += 1
        else:
            series = {"15m": cs, "30m": to_30m(cs)}
            for bar in C.CONFIRM_TIMEFRAMES:
                ltf = TF_MS[bar]
                for c in series.get(bar, []):
                    if c["closed"] and c["ts"] >= start and c["ts"] + ltf <= end:
                        events.append((c["ts"] + ltf, bar, c))
        events.sort(key=lambda e: (e[0], e[1]))

        outcome = None
        for close_ts, bar, c in events:
            if c["c"] > w["h"]:
                outcome = ("cancel", close_ts, bar, c)
                break
            if breakdown(c, w):
                outcome = ("confirm", close_ts, bar, c)
                break

        if outcome is None:
            if (cs is not None and now >= end + 20 * 60_000) or now >= end + tf_ms:
                log(f"  {w['inst']} {w['tf']}: свеча 2 закончилась без падения, снимаю с наблюдения")
                del state["watch"][key]
                stats["expired"] += 1
            continue

        kind, close_ts, bar, c = outcome
        if kind == "cancel":
            log(f"  {w['inst']} {w['tf']}: закрытие выше максимума звезды, сценарий отменён")
            del state["watch"][key]
            stats["cancelled"] += 1
            continue

        rng = w["h"] - w["l"]
        if level_of(w) - c["c"] > C.MAX_CHASE_RANGES * rng:
            log(f"  {w['inst']} {w['tf']}: падение уже ушло слишком далеко, не отправляю")
            del state["watch"][key]
            stats["too_late"] += 1
            continue
        last = state["last_alert"].get(w["inst"])
        if last and now - last < C.COIN_COOLDOWN_HOURS * 3_600_000:
            log(f"  {w['inst']}: по этой монете недавно уже было сообщение, пропускаю")
            del state["watch"][key]
            stats["cooldown"] += 1
            continue
        ctx = {"cur": cs[-1]["c"], "now": now_ms(),
               "rsi_now": rsi([x["c"] for x in cs]),
               "vol_break": vol_mult_at(series[bar], c["ts"])}
        if send(build_message(w, c, bar, close_ts, ctx)):
            state["last_alert"][w["inst"]] = now
            del state["watch"][key]
            stats["sent"] += 1
            log(f"  ОТПРАВЛЕНО: {w['inst']} (звезда {w['tf']}, начало на {bar})")
        else:
            stats["send_failed"] += 1


def main():
    if "--test" in sys.argv or os.environ.get("TEST_TELEGRAM") == "1":
        ok = send(f"✅ {C.TITLE}: бот на связи. Сообщения о звёздах будут приходить сюда.")
        log("Тестовое сообщение отправлено" if ok else "Тестовое сообщение НЕ отправлено")
        sys.exit(0 if ok else 1)

    state = load_state()
    now = now_ms()
    stats = {"scanned": {}, "failed": 0, "found": 0, "errors": 0, "expired": 0, "cancelled": 0,
             "too_late": 0, "cooldown": 0, "sent": 0, "send_failed": 0}
    new_keys = set()
    try:
        check_watch(state, now, stats)                      # 1) срочно: звёзды, найденные раньше
        scan_new_stars(state, now, stats, new_keys)         # 2) долго: поиск новых звёзд
        if new_keys:                                        # 3) новые звёзды - вдруг запуск сильно опоздал
            check_watch(state, now_ms(), stats, only=new_keys)
        prune(state, now)
    finally:
        save_state(state)
    log(f"Итог: проверено монет {stats['scanned'] or 'новых свечей нет'}, ошибок запросов {stats['failed']}, "
        f"новых звёзд {stats['found']}, на наблюдении {len(state['watch'])}, отправлено {stats['sent']}, "
        f"отменено {stats['cancelled']}, не успели {stats['expired']}, слишком поздно {stats['too_late']}, "
        f"повтор по монете {stats['cooldown']}, время работы {(time.time() - T0) / 60:.1f} мин")
    if stats["send_failed"]:
        sys.exit(1)


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        s = {}
    for k in ("scanned", "watch", "seen", "last_alert"):
        s.setdefault(k, {})
    return s


def save_state(s):
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False)


def prune(state, now):
    state["seen"] = {k: v for k, v in state["seen"].items() if now - v < 3 * DAY_MS}
    state["last_alert"] = {k: v for k, v in state["last_alert"].items() if now - v < 2 * DAY_MS}


if __name__ == "__main__":
    main()
