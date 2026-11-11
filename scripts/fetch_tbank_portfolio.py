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

TLS: API tbank.ru отдаёт цепочку от корня Минцифры «Russian Trusted Root CA»,
которого нет в хранилище сертификатов GitHub-раннеров (и многих Linux-систем)
— без встроенного корня urllib падает с
«SSL: CERTIFICATE_VERIFY_FAILED ... self-signed certificate in certificate chain».
Корень и Sub CA встроены ниже как cadata (публичные сертификаты Госуслуг,
отпечаток корня сверяется при каждом запуске). Если Т-Банк сменит цепочку,
скачайте свежий корень
  https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt
и укажите его: export TB_ROOT_CERT=/путь/russian_trusted_root_ca.pem
"""
import argparse
import base64
import hashlib
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

BASE = "https://invest-public-api.tbank.ru/rest/tinkoff.public.invest.api.contract.v1"
TOKEN = os.environ.get("TB_TOKEN", "")

# ------------------------------------------------------------- TLS / корень Минцифры
# Корневой сертификат «Russian Trusted Root CA» (Минцифры), получен с Госуслуг,
# отпечаток SHA-256 сверяется с официальным (gu-st.ru) при каждом запуске.
RU_ROOT_CA_PEM = """-----BEGIN CERTIFICATE-----
MIIFwjCCA6qgAwIBAgICEAAwDQYJKoZIhvcNAQELBQAwcDELMAkGA1UEBhMCUlUx
PzA9BgNVBAoMNlRoZSBNaW5pc3RyeSBvZiBEaWdpdGFsIERldmVsb3BtZW50IGFu
ZCBDb21tdW5pY2F0aW9uczEgMB4GA1UEAwwXUnVzc2lhbiBUcnVzdGVkIFJvb3Qg
Q0EwHhcNMjIwMzAxMjEwNDE1WhcNMzIwMjI3MjEwNDE1WjBwMQswCQYDVQQGEwJS
VTE/MD0GA1UECgw2VGhlIE1pbmlzdHJ5IG9mIERpZ2l0YWwgRGV2ZWxvcG1lbnQg
YW5kIENvbW11bmljYXRpb25zMSAwHgYDVQQDDBdSdXNzaWFuIFRydXN0ZWQgUm9v
dCBDQTCCAiIwDQYJKoZIhvcNAQEBBQADggIPADCCAgoCggIBAMfFOZ8pUAL3+r2n
qqE0Zp52selXsKGFYoG0GM5bwz1bSFtCt+AZQMhkWQheI3poZAToYJu69pHLKS6Q
XBiwBC1cvzYmUYKMYZC7jE5YhEU2bSL0mX7NaMxMDmH2/NwuOVRj8OImVa5s1F4U
zn4Kv3PFlDBjjSjXKVY9kmjUBsXQrIHeaqmUIsPIlNWUnimXS0I0abExqkbdrXbX
YwCOXhOO2pDUx3ckmJlCMUGacUTnylyQW2VsJIyIGA8V0xzdaeUXg0VZ6ZmNUr5Y
Ber/EAOLPb8NYpsAhJe2mXjMB/J9HNsoFMBFJ0lLOT/+dQvjbdRZoOT8eqJpWnVD
U+QL/qEZnz57N88OWM3rabJkRNdU/Z7x5SFIM9FrqtN8xewsiBWBI0K6XFuOBOTD
4V08o4TzJ8+Ccq5XlCUW2L48pZNCYuBDfBh7FxkB7qDgGDiaftEkZZfApRg2E+M9
G8wkNKTPLDc4wH0FDTijhgxR3Y4PiS1HL2Zhw7bD3CbslmEGgfnnZojNkJtcLeBH
BLa52/dSwNU4WWLubaYSiAmA9IUMX1/RpfpxOxd4Ykmhz97oFbUaDJFipIggx5sX
ePAlkTdWnv+RWBxlJwMQ25oEHmRguNYf4Zr/Rxr9cS93Y+mdXIZaBEE0KS2iLRqa
OiWBki9IMQU4phqPOBAaG7A+eP8PAgMBAAGjZjBkMB0GA1UdDgQWBBTh0YHlzlpf
BKrS6badZrHF+qwshzAfBgNVHSMEGDAWgBTh0YHlzlpfBKrS6badZrHF+qwshzAS
BgNVHRMBAf8ECDAGAQH/AgEEMA4GA1UdDwEB/wQEAwIBhjANBgkqhkiG9w0BAQsF
AAOCAgEAALIY1wkilt/urfEVM5vKzr6utOeDWCUczmWX/RX4ljpRdgF+5fAIS4vH
tmXkqpSCOVeWUrJV9QvZn6L227ZwuE15cWi8DCDal3Ue90WgAJJZMfTshN4OI8cq
W9E4EG9wglbEtMnObHlms8F3CHmrw3k6KmUkWGoa+/ENmcVl68u/cMRl1JbW2bM+
/3A+SAg2c6iPDlehczKx2oa95QW0SkPPWGuNA/CE8CpyANIhu9XFrj3RQ3EqeRcS
AQQod1RNuHpfETLU/A2gMmvn/w/sx7TB3W5BPs6rprOA37tutPq9u6FTZOcG1Oqj
C/B7yTqgI7rbyvox7DEXoX7rIiEqyNNUguTk/u3SZ4VXE2kmxdmSh3TQvybfbnXV
4JbCZVaqiZraqc7oZMnRoWrXRG3ztbnbes/9qhRGI7PqXqeKJBztxRTEVj8ONs1d
WN5szTwaPIvhkhO3CO5ErU2rVdUr89wKpNXbBODFKRtgxUT70YpmJ46VVaqdAhOZ
D9EUUn4YaeLaS8AjSF/h7UkjOibNc4qVDiPP+rkehFWM66PVnP1Msh93tc+taIfC
EYVMxjh8zNbFuoc7fzvvrFILLe7ifvEIUqSVIC/AzplM/Jxw7buXFeGP1qVCBEHq
391d/9RAfaZ12zkwFsl+IKwE/OZxW8AHa9i1p4GO0YSNuczzEm4=
-----END CERTIFICATE-----
"""

# Промежуточный «Russian Trusted Sub CA» — добавлен, чтобы цепочка собиралась
# даже если сервер перестанет отдавать промежуточный сертификат.
RU_SUB_CA_PEM = """-----BEGIN CERTIFICATE-----
MIIG6DCCBNCgAwIBAgICEAUwDQYJKoZIhvcNAQELBQAwcDELMAkGA1UEBhMCUlUx
PzA9BgNVBAoMNlRoZSBNaW5pc3RyeSBvZiBEaWdpdGFsIERldmVsb3BtZW50IGFu
ZCBDb21tdW5pY2F0aW9uczEgMB4GA1UEAwwXUnVzc2lhbiBUcnVzdGVkIFJvb3Qg
Q0EwHhcNMjQwNzE1MTI1MDQxWhcNMjkwNzE5MTI1MDQxWjBvMQswCQYDVQQGEwJS
VTE/MD0GA1UECgw2VGhlIE1pbmlzdHJ5IG9mIERpZ2l0YWwgRGV2ZWxvcG1lbnQg
YW5kIENvbW11bmljYXRpb25zMR8wHQYDVQQDDBZSdXNzaWFuIFRydXN0ZWQgU3Vi
IENBMIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEA1j0rkZECOt1S8o7I
JY+4YKAxuEa5xaHKHXT2EpkuC/0krqMOjUy2oPIRNgR5g8X0Jl6jamxeGLc4Q1tf
ju6or9oSRYThIUhRsFDQNBiBBEXoBgWxTfiKB2eyT97+pz5TBtBiRCPaLGRHYLRb
9Jz2HkJlxbtNPjtDrF5DPHym+mZ1M1z3hIQYAqJwLpsEBnsw/VxWMlxqHoeewd0h
uJMd71KQ5vOKlz7KrIZ6EobNNa6wItuvsfj3kYCK7O78uLHGXXFxdr8Hae9lMUmC
8F7AFwa+bO1LRlTlqW7rE3rLf+jj70N01N8T3o22v14YBaFBWQWncAVYD2JuL3tH
252+kdNOERf1fLbLRigJAbd+hOhWYlNf963TFDgnNPliHNIW72SygVBnI2V3JwO1
dp1hVKpK/zt8ziGdHW4gmOLTsH50YKdR4jNqUgQv4wASlKn9OpN6zHYc5G8h86fY
BM+zxE5ikGI+I/vIqBuI0eaDU92AWN/YjFLpu8tMu9kLRSCf1vug6FIfDPWVo7iP
ac/SI2v8jnnpaW7ph/Pz3WkzaG7ZZJsfFs+8dploWc6LOoDtbFBhMdGMxu024msC
0PSjZb5ODXPIaO2NsA7fMiAtZcoK6anTUJh4zOP/stA9qsJGNxdrEmiPXSmBZY/N
Y0wkZgZ6JTDhw7038bPvctkblJkCAwEAAaOCAYswggGHMB0GA1UdDgQWBBR3Pdk5
r0K93FvKduru/c4+YSkwXzAfBgNVHSMEGDAWgBTh0YHlzlpfBKrS6badZrHF+qws
hzAOBgNVHQ8BAf8EBAMCAYYwEgYDVR0TAQH/BAgwBgEB/wIBADCBmAYIKwYBBQUH
AQEEgYswgYgwQAYIKwYBBQUHMAKGNGh0dHA6Ly9udWMtY2RwLnZvc2tob2QucnUv
Y2RwL3Jvb3RjYV9zc2xfcnNhMjAyMi5jcnQwRAYIKwYBBQUHMAKGOGh0dHA6Ly9u
dWMtY2RwLmRpZ2l0YWwuZ292LnJ1L2NkcC9yb290Y2Ffc3NsX3JzYTIwMjIuY3J0
MIGFBgNVHR8EfjB8MDqgOKA2hjRodHRwOi8vbnVjLWNkcC52b3NraG9kLnJ1L2Nk
cC9yb290Y2Ffc3NsX3JzYTIwMjIuY3JsMD6gPKA6hjhodHRwOi8vbnVjLWNkcC5k
aWdpdGFsLmdvdi5ydS9jZHAvcm9vdGNhX3NzbF9yc2EyMDIyLmNybDANBgkqhkiG
9w0BAQsFAAOCAgEAmsINXtQ7wwUWvIeOr80MdJS/5G4xhyZOVEmeUorThquT672y
cCg3XCxc4fwbiZqSSbBqntQ7RtiTAKMYMvBageKoVHbzz+R4jX01tKcTx8cDePrz
dJ73bLNUorE7RU9QsW4KyiUeRmjMDV23AUlEvuQFTwgkHXvbac1BBdPn9CrssQuF
5EGohZKcQPFiAAc4SHbRNhlr7uAwgpc/erzI9EAcvA6BVAXcVKoeGpV01uexUgZ6
St5RP9UmDWNA7T4yVXWJ233N0Q8bl+6AswINQ3PosPu6yQQHQjr65YS06epK+AeI
6j+oGR4xI7EhTQhQvaobnGmX/8QQ7XDRYCP2HXYxiffnn/CfZ/BVyKLYeY1ZipjE
nzqdQIC2+Q3WtY8jsVRQMP38WFRmtsIt5snehnPTs5bKGVIcYzj3o3Ex/K7agEz0
zAJ0JR5ivXZOvNkT0g9x1v+S1IkU3e/nX1a+tpRquMtnHX0L2lXArNHUbaOO9EJt
d57WaIpofV5cVhhwShOgAuBc9UMJF3/n4t4RKiPxtsK8P67gcmphMhslj7AMYrYM
ej2NvQZY4m3ub3CPC/PrTjDONvb+8g5xrKtxBjYqC74HSB4dg9G3WimSDUuP2Su6
G2y2TUeyJuCvCLz289VoO0vg7cNdMobE3KCqAiiNhN2VBFxHAUKmUoRcRdw=
-----END CERTIFICATE-----
"""

RU_ROOT_CA_SHA256 = ("D2:6D:2D:02:31:B7:C3:9F:92:CC:73:85:12:BA:54:10:35:19:"
                     "E4:40:5D:68:B5:BD:70:3E:97:88:CA:8E:CF:31")

_SSL_CTX = None


def _pem_sha256(pem):
    """SHA-256 PEM-сертификата в формате XX:XX:..."""
    der = base64.b64decode("".join(
        l for l in pem.strip().splitlines() if "-----" not in l))
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def ssl_context():
    """Контекст по умолчанию + встроенный корень Минцифры (+ файл из TB_ROOT_CERT)."""
    global _SSL_CTX
    if _SSL_CTX is None:
        ctx = ssl.create_default_context()
        fingerprint = _pem_sha256(RU_ROOT_CA_PEM)
        if fingerprint != RU_ROOT_CA_SHA256:
            sys.exit(
                "Встроенный корневой сертификат Russian Trusted Root CA не совпадает "
                "с ожидаемым отпечатком:\n"
                f"  ожидается: {RU_ROOT_CA_SHA256}\n"
                f"  получено:  {fingerprint}\n"
                "Обновите fetch_tbank_portfolio.py или укажите свой корень: "
                "export TB_ROOT_CERT=/путь/russian_trusted_root_ca.pem")
        ctx.load_verify_locations(cadata=RU_ROOT_CA_PEM + "\n" + RU_SUB_CA_PEM)
        extra = os.environ.get("TB_ROOT_CERT", "")
        if extra:
            try:
                ctx.load_verify_locations(cafile=extra)
            except (ssl.SSLError, ValueError, OSError) as e:
                sys.exit(f"TB_ROOT_CERT={extra}: не читается как PEM-сертификат ({e})")
            print(f"TLS: добавлен внешний корневой сертификат {extra}")
        _SSL_CTX = ctx
    return _SSL_CTX


def call(method, payload, tries=3):
    """POST к REST-шлюзу T-Invest API c ретраем на 429/5xx."""
    if not TOKEN:
        sys.exit("Ошибка: не задан токен. export TB_TOKEN='t.xxx' (read-only токен).")
    data = json.dumps(payload).encode("utf-8")
    for attempt in range(1, tries + 1):
        req = urllib.request.Request(
            # Полное имя сервиса через ТОЧКУ: ...v1.UsersService/GetAccounts
            # (со слешем шлюз отдаёт HTTP 404).
            f"{BASE}.{method}",
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
            with urllib.request.urlopen(req, timeout=30, context=ssl_context()) as resp:
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
        except urllib.error.URLError as e:
            if isinstance(getattr(e, "reason", None), ssl.SSLError):
                sys.exit(
                    f"Ошибка TLS к {BASE}: {e.reason}\n"
                    "В этом скрипте встроен корневой сертификат Минцифры "
                    "(Russian Trusted Root CA). Если он устарел — обновите скрипт "
                    "или скачайте свежий корень с Госуслуг и укажите его: "
                    "export TB_ROOT_CERT=/путь/russian_trusted_root_ca.pem")
            raise  # DNS/сеть — не наша тема, пусть падает как есть
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
# API отдаёт тип инструмента в НИЖНЕМ регистре (share/bond/etf/currency) —
# приводим к каноническому виду Share/Bond/Etf/Currency (как на странице).
TYPE_MAP = {"share": "Share", "bond": "Bond", "etf": "Etf", "currency": "Currency",
            "future": "Future", "option": "Option", "sp": "Sp"}
by_type = {}
for p in pf["positions"]:
    qty = dec(p.get("quantity")) or 0
    if qty == 0:
        continue  # нулевые позиции не нужны
    raw_type = (p.get("instrumentType") or "?").strip()
    itype = TYPE_MAP.get(raw_type.lower(), raw_type)
    p["instrumentType"] = itype          # нормализуем и в самой позиции
    by_type.setdefault(itype, []).append(p)


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

        # Цена облигации в GetPortfolio приходит В РУБЛЯХ за бумагу, без НКД
        # (проверено на живых данных: totalAmountBonds = Σ qty×(цена + НКД);
        # пример: цена 997,00 ₽ + НКД 14,58 = 1011,58 ₽ × 30 шт = 30 347,40 = totalAmountBonds).
        # В старых версиях API цена была в % от номинала — обрабатываем и этот формат:
        # если цена меньше 10% номинала, считаем её процентной записью.
        price_note = ""
        if itype == "Bond" and cur_price is not None:
            if nominal and abs(cur_price) < nominal / 10:
                price_note = f"{cur_price:g}% ном. {nominal:g}"
                cur_price = cur_price / 100 * nominal + (nkd or 0)
            else:
                price_note = (f"{cur_price:g} ₽" +
                              (f" + НКД {nkd:g}" if nkd else ""))
                cur_price = cur_price + (nkd or 0)  # цена сайта — с НКД, как в приложении Т-Банка

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
