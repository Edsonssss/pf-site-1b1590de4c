#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Выгрузка портфеля Т-Банк через T-Invest API (REST, только стандартная библиотека).

Что делает:
  1. UsersService/GetAccounts        — список счетов, выбор счёта
  2. OperationsService/GetPortfolio  — позиции: кол-во, средняя цена, текущая цена, НКД
  3. InstrumentsService/*By          — FIGI -> тикер/имя; для облигаций: номинал
                                       (цена в API — % от номинала, как на бирже)
  4. Печатает таблицу, сохраняет tbank_portfolio.json
  5. Если рядом лежит portfolio_data.json (JSON сайта) — сверяет количества

Использование:
  export TB_TOKEN="t.xxxxxxxxxxxxxxxxxxxx"     # read-only токен T-Invest API
  python3 fetch_tbank_portfolio.py             # все счёта -> выбрать
  python3 fetch_tbank_portfolio.py --account 2000123456 --out tbank_portfolio.json
  python3 fetch_tbank_portfolio.py --site portfolio_data.json   # сверка с сайтом

Лимиты API: OperationsService — 100 REST-запросов/мин (нам нужно ~30).
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "https://invest-public-api.tbank.ru/rest/tinkoff.public.invest.api.contract.v1"
TOKEN = os.environ.get("TB_TOKEN", "")


def call(method, payload, tries=3):
    """POST к REST-шлюзу T-Invest API c ретраем на 429/5xx."""
    if not TOKEN:
        sys.exit("Ошибка: не задан токен. export TB_TOKEN='t.xxx' (read-only токен).")
    data = json.dumps(payload).encode("utf-8")
    for attempt in range(1, tries + 1):
        req = urllib.request.Request(
            f"{BASE}/{method}",
            data=data,
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/json",
                "User-Agent": "portfolio-sync/1.0",
                "x-app-name": "portfolio-sync",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            if e.code == 429 and attempt < tries:
                time.sleep(20 * attempt)
                continue
            if e.code in (500, 502, 503, 504) and attempt < tries:
                time.sleep(5 * attempt)
                continue
            sys.exit(f"Ошибка API {method}: HTTP {e.code}: {body}\n"
                     f"(40003 = токен неактуален; 80000-х = лимиты)")
    sys.exit(f"Не удалось выполнить {method} после {tries} попыток")


def dec(q):
    """Quotation/MoneyValue (units + nano) -> float. units может быть строкой."""
    if not q:
        return None
    units = q.get("units", 0)
    nano = q.get("nano", 0)
    return int(units or 0) + int(nano or 0) / 1e9


def money(m):
    """MoneyValue -> (float, currency)."""
    if not m:
        return None, None
    return dec(m), m.get("currency")


# ---------------------------------------------------------------- 1. счёт
accounts = call("UsersService/GetAccounts", {})["accounts"]
print("Счета:")
for a in accounts:
    print(f"  {a['id']}  {a.get('type')}  {a.get('name', '')}  access={a.get('accessLevel')}")

ap = argparse.ArgumentParser()
ap.add_argument("--account", help="id счёта (иначе первый брокерский)")
ap.add_argument("--out", default="tbank_portfolio.json")
ap.add_argument("--site", default="", help="путь к portfolio_data.json сайта для сверки")
args = ap.parse_args()

acc = args.account or os.environ.get("TB_ACCOUNT")
if not acc:
    broker = [a for a in accounts if a.get("type") == "ACCOUNT_TYPE_TINKOFF"]
    acc = (broker or accounts)[0]["id"]
    print(f"\nВыбран счёт: {acc} (можно переопределить: --account <id>)\n")

# ------------------------------------------------------------- 2. портфель
pf = call("OperationsService/GetPortfolio", {"accountId": acc})

totals = {}
for key, label in [("totalAmountShares", "Акции"), ("totalAmountBonds", "Облигации"),
                   ("totalAmountEtf", "Фонды"), ("totalAmountCurrencies", "Валюта"),
                   ("totalAmountFutures", "Фьючерсы"), ("totalAmountPortfolio", "ВЕСЬ портфель")]:
    v, cur = money(pf.get(key))
    if v:
        totals[key] = {"value": v, "currency": cur}
        print(f"  {label:12} {v:>14,.2f} {cur or ''}")

ey = dec(pf.get("expectedYield"))
if ey is not None:
    print(f"  Доходность портфеля (expectedYield): {ey:+,.2f}")

# ------------------------------------------------- 3. FIGI -> тикер / номинал
by_type = {}
for p in pf["positions"]:
    qty = dec(p.get("quantity")) or 0
    if qty == 0:
        continue  # нулевые позиции не нужны
    by_type.setdefault(p.get("instrumentType"), []).append(p)


def instrument_by(method, figi):
    """InstrumentsService/ShareBy|BondBy|EtfBy|CurrencyBy -> instrument."""
    try:
        return call(f"InstrumentsService/{method}", {"idType": "FIGI", "id": figi})["instrument"]
    except SystemExit:
        raise
    except Exception:
        return {}


rows = []
for itype, positions in sorted(by_type.items()):
    for p in positions:
        figi = p["figi"]
        qty = dec(p.get("quantity"))
        cur_price, cur_ccy = money(p.get("currentPrice"))
        avg_price, avg_ccy = money(p.get("averagePositionPriceFifo") or p.get("averagePositionPrice"))
        nkd, _ = money(p.get("currentNkd"))

        inst, nominal = {}, None
        if itype == "Share":
            inst = instrument_by("ShareBy", figi)
        elif itype == "Bond":
            inst = instrument_by("BondBy", figi)
            nominal, _ = money(inst.get("nominal"))
        elif itype == "Etf":
            inst = instrument_by("EtfBy", figi)
        elif itype == "Currency":
            inst = instrument_by("CurrencyBy", figi)

        # У облигаций currentPrice — % от номинала: приводим к рублям за бумагу + НКД
        price_note = ""
        if itype == "Bond" and cur_price is not None and nominal:
            price_note = f"{cur_price:g}% ном. {nominal:g}"
            cur_price = cur_price / 100 * nominal + (nkd or 0)

        rows.append({
            "figi": figi,
            "ticker": inst.get("ticker") or "",
            "isin": inst.get("isin") or "",
            "name": inst.get("name") or inst.get("shortName") or "",
            "instrumentType": itype,
            "currency": cur_ccy or avg_ccy or inst.get("currency"),
            "qty": qty,
            "price": round(cur_price, 4) if cur_price is not None else None,
            "price_raw_pct_nominal": price_note or None,
            "nominal": nominal,
            "nkd": round(nkd, 2) if nkd else None,
            "avg_price": round(avg_price, 4) if avg_price is not None else None,
            "value": round(qty * cur_price, 2) if cur_price is not None else None,
            "expected_yield": round(dec(p.get("expectedYield")), 2) if dec(p.get("expectedYield")) is not None else None,
            "blocked": p.get("blocked", False),
        })
        time.sleep(0.15)  # щадящий темп: ~8 запросов/мин при 30 позициях, лимит 100/мин

rows.sort(key=lambda r: (r["instrumentType"], -(r["value"] or 0)))

# --------------------------------------------------------------- 4. вывод
print(f"\n{'Тикер':10} {'Тип':9} {'Кол-во':>12} {'Цена':>12} {'Стоимость':>14}  Название")
for r in rows:
    px = f"{r['price']:,.4f}".rstrip("0").rstrip(".") if r["price"] is not None else "?"
    val = f"{r['value']:,.2f}" if r["value"] is not None else "?"
    print(f"  {r['ticker'] or r['figi']:8} {r['instrumentType']:8} {r['qty']:>12,.0f} {px:>12} {val:>14}  {r['name'][:40]}")

result = {"account": acc, "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
          "totals": totals, "expected_yield": ey, "positions": rows}
with open(args.out, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=1)
print(f"\nСохранено: {args.out} ({len(rows)} позиций)")

# ------------------------------------------------------- 5. сверка с сайтом
site = args.site or ("portfolio_data.json" if os.path.exists("portfolio_data.json") else "")
if site and os.path.exists(site):
    sdata = json.load(open(site, encoding="utf-8"))
    smap = {s["ticker"]: s for s in sdata.get("stocks", [])}
    print("\nСверка с сайтом", site, ":")
    found_any = False
    for r in rows:
        if r["instrumentType"] not in ("Share",):
            continue
        t = r["ticker"]
        if not t:
            continue
        s = smap.get(t)
        if not s:
            print(f"  {t:8}: есть в Т-Банке ({r['qty']:g} шт), на сайте НЕТ")
            found_any = True
        elif abs(s["qty"] - r["qty"]) > 1e-9:
            print(f"  {t:8}: сайт {s['qty']:g} шт -> Т-Банк {r['qty']:g} шт "
                  f"(разница {r['qty'] - s['qty']:+g})")
            found_any = True
    for t, s in smap.items():
        if t not in {r["ticker"] for r in rows if r["instrumentType"] == "Share"}:
            print(f"  {t:8}: есть на сайте ({s['qty']:g} шт), в Т-Банке НЕТ")
            found_any = True
    if not found_any:
        print("  Расхождений нет ✓")
