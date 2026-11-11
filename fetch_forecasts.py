#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ежедневное обновление сводных прогнозов Т-Банка через официальный T-Invest API.

Предыдущая версия выкачивала страницы tbank.ru/invest/stocks/<тикер>/ и выта-
скивала консенсус из HTML — Т-Банк начал ограничивать такие запросы
(HTTP 429 Too Many Requests после нескольких страниц). Теперь данные берутся
из официального API, троттлинг-лимиты там задокументированы и нам достаточно
2–5 запросов на всё обновление:

  InstrumentsService/GetConsensusForecasts — консенсус-прогнозы по всем
      инструментам сразу (постранично, ~1–2 запроса):
        bestTargetPrice            — средняя целевая цена
        bestTargetLow / High       — диапазон целей (min/max)
        totalBuy/Hold/SellRecommend — число аналитиков по каждой рекомендации
        consensus                  — RECOMMENDATION_BUY / HOLD / SELL
        prognosisDate              — дата прогноза
  InstrumentsService/Shares — сопоставление тикер -> UID (1 запрос);
      для недостающих тикеров — резервный InstrumentsService/FindInstrument.

Обновляет в portfolio_data.json по каждой акции: recommendation (русские
метки: Покупать/Держать/Продавать), target_price, range_min, range_max,
n_forecasts (суммарное число аналитиков), prognosis_date, upside_pct
(по текущей цене из JSON), summary.recommendations, meta.forecasts_date и
meta.sources.forecasts. Бумаги без консенсуса (сейчас FEES, PMSB) и облигации
(ISIN) не трогаются; при сетевом сбое прогноз остаётся прежним и сценарий не
падает (дневное обновление цен продолжается).

Использование:
  export TB_TOKEN="t.xxxxx"          # read-only токен T-Invest API (или --token)
  python3 fetch_forecasts.py
  python3 fetch_forecasts.py --data путь/к/portfolio_data.json

Windows (cmd):  set TB_TOKEN=t.xxxxx
Windows (PowerShell): $env:TB_TOKEN="t.xxxxx"

В workflow daily-update токен подставляется из repository secret TB_TOKEN
(тот же секрет, что и для выгрузки портфеля).
"""
import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

BASE = "https://invest-public-api.tbank.ru/rest/tinkoff.public.invest.api.contract.v1"
TOKEN = os.environ.get("TB_TOKEN", "")
SKIP = {"PINS-RM"}                       # бумага без консенсуса, как и в auto_update.py
REC_LABEL = {"RECOMMENDATION_BUY": "Покупать",
             "RECOMMENDATION_HOLD": "Держать",
             "RECOMMENDATION_SELL": "Продавать"}
BUCKET = {"Покупать": "buy", "Держать": "hold", "Продавать": "sell"}


def call(method, payload, tries=3, token=None):
    """POST к REST-шлюзу T-Invest API c ретраем на 429/5xx (как в fetch_tbank_portfolio)."""
    tk = token if token is not None else TOKEN
    if not tk:
        sys.exit("Ошибка: не задан токен. export TB_TOKEN='t.xxx' "
                 "(read-only токен T-Invest API, тот же, что для выгрузки портфеля).")
    data = json.dumps(payload).encode("utf-8")
    for attempt in range(1, tries + 1):
        req = urllib.request.Request(
            f"{BASE}/{method}",
            data=data,
            headers={
                "Authorization": f"Bearer {tk}",
                "Content-Type": "application/json",
                "User-Agent": "portfolio-forecasts/1.0",
                "x-app-name": "portfolio-forecasts",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            if e.code in (401, 403):
                sys.exit(f"Ошибка API {method}: HTTP {e.code}: {body}\n"
                         f"Токен неактуален или у него нет доступа к InstrumentsService — "
                         f"проверьте secret TB_TOKEN.")
            if e.code == 429 and attempt < tries:
                time.sleep(20 * attempt)
                continue
            if e.code in (500, 502, 503, 504) and attempt < tries:
                time.sleep(5 * attempt)
                continue
            sys.exit(f"Ошибка API {method}: HTTP {e.code}: {body}")
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt < tries:
                time.sleep(5 * attempt)
                continue
            raise


def dec(q):
    """Quotation (units + nano) -> float; units приходит строкой."""
    if not q:
        return None
    try:
        return int(q.get("units") or 0) + int(q.get("nano") or 0) / 1e9
    except (TypeError, ValueError):
        return None


def is_isin(t):
    """Облигации (ISIN вроде RU000A10C5L7) — консенсуса по ним нет."""
    return re.match(r"^RU[0-9A-Z]{10}$", t) is not None


def ticker_to_uid(stocks, token=None):
    """Тикер -> UID инструмента: одним Shares, недостающие — через FindInstrument."""
    t2u = {}
    shares = call("InstrumentsService/Shares",
                  {"instrumentStatus": "INSTRUMENT_STATUS_BASE"}, token=token).get("instruments", [])
    for it in shares:
        t = it.get("ticker")
        if not t:
            continue
        cur = t2u.get(t)
        # приоритет основному борду TQBR, если тикер встречается в нескольких секциях
        if cur is None or (it.get("classCode") == "TQBR" and cur[1] != "TQBR"):
            t2u[t] = (it.get("uid"), it.get("classCode"))
    need = [s["ticker"] for s in stocks if s["ticker"] not in t2u]
    for t in need:
        try:
            r = call("InstrumentsService/FindInstrument", {"query": t}, token=token).get("instruments", [])
        except Exception as e:
            print(f"  {t}: поиск UID не удался — {e}")
            continue
        hits = [i for i in r if i.get("ticker") == t]
        hit = next((i for i in hits if i.get("classCode") == "TQBR"), hits[0] if hits else None)
        if hit and hit.get("uid"):
            t2u[t] = (hit["uid"], hit.get("classCode"))
    return t2u


def load_consensus(token=None):
    """UID -> запись консенсуса (постранично). Останов: пустая страница или всё собрано."""
    items, page = {}, 0
    total = None
    while True:
        resp = call("InstrumentsService/GetConsensusForecasts",
                    {"paging": {"limit": 200, "pageNumber": page}}, token=token)
        chunk = resp.get("items") or []
        for it in chunk:
            items[it.get("uid")] = it
        pr = resp.get("page") or {}
        total = pr.get("totalCount") if pr.get("totalCount") is not None else total
        page += 1
        if not chunk or (total is not None and len(items) >= total) or page > 60:
            break
        time.sleep(0.2)
    return items


def main(argv=None):
    ap = argparse.ArgumentParser(description="Обновление прогнозов Т-Банка в portfolio_data.json")
    ap.add_argument("--data", default="portfolio_data.json",
                    help="путь к portfolio_data.json (по умолчанию — рядом со скриптом)")
    args = ap.parse_args(argv)

    path = args.data if os.path.isabs(args.data) else os.path.join(BASE_DIR, args.data)
    d = json.load(open(path, encoding="utf-8"))
    today = datetime.date.today()
    targets = [s for s in d["stocks"] if s["ticker"] not in SKIP and not is_isin(s["ticker"])]

    try:
        t2u = ticker_to_uid(targets)
        items = load_consensus()
    except Exception as e:
        print(f"ВНИМАНИЕ: консенсус не загружен ({e}) — прогнозы оставлены прежними; "
              f"дневное обновление продолжается")
        return

    updated, moved, empty = [], [], []
    for s in targets:
        t = s["ticker"]
        pair = t2u.get(t)
        item = items.get(pair[0]) if pair else None
        target = dec(item.get("bestTargetPrice")) if item else None
        if not item or target is None:
            empty.append(t)
            print(f"  {t}: консенсуса в API нет — оставляем прежнее")
            continue
        old = s.get("target_price")
        s["recommendation"] = REC_LABEL.get(item.get("consensus") or "")
        s["target_price"] = target
        s["range_min"] = dec(item.get("bestTargetLow"))
        s["range_max"] = dec(item.get("bestTargetHigh"))
        s["n_forecasts"] = (int(item.get("totalBuyRecommend") or 0)
                            + int(item.get("totalHoldRecommend") or 0)
                            + int(item.get("totalSellRecommend") or 0))
        prog = (item.get("prognosisDate") or "")[:10]
        if prog:
            s["prognosis_date"] = prog
        price = s.get("current_price")
        s["upside_pct"] = (round((target / price - 1) * 100, 2) if price else None)
        updated.append(t)
        extra = "" if old == target else f"  (было {old})"
        if old != target:
            moved.append(f"{t}: {old} → {target}")
        print(f"  {t}: {s['recommendation']}, цель {target} ₽ "
              f"[{s['range_min']} — {s['range_max']}], аналитиков {s['n_forecasts']}{extra}")

    if updated:
        recs = {"buy": 0, "hold": 0, "sell": 0, "no_forecast": 0}
        for s in d["stocks"]:
            recs[BUCKET.get(s.get("recommendation"), "no_forecast")] += 1
        d["summary"]["recommendations"] = recs
        d["meta"]["forecasts_date"] = str(today)
        d["meta"]["sources"]["forecasts"] = (
            "T-Invest API (консенсус-прогнозы аналитиков) — на "
            + today.strftime("%d.%m.%Y") + " (автообновление GitHub Actions)")
        json.dump(d, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("обновлено прогнозов:", len(updated), "| цели изменились:", len(moved),
          "| без консенсуса:", ", ".join(empty) or "нет")
    for x in moved:
        print("  цель сменилась:", x)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

if __name__ == "__main__":
    main()
