#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ежедневное обновление сводных прогнозов Т-Банка через официальный T-Invest API.

Предыдущая версия выкачивала страницы tbank.ru/invest/stocks/<тикер>/ и выта-
скивала консенсус из HTML — Т-Банк начал ограничивать такие запросы
(HTTP 429 Too Many Requests после нескольких страниц). Теперь данные берутся
из официального API, троттлинг-лимиты там задокументированы:

  InstrumentsService/FindInstrument — тикер -> UID инструмента, отдельный
      маленький запрос на каждый тикер (~25 шт). Раньше карта тикеров
      строилась одним запросом InstrumentsService/Shares, но его ответ
      весит ~3 МБ, и на раннере GitHub Actions передача обрывалась на
      середине (IncompleteRead) — из-за этого весь шаг оставлял прогнозы
      прежними. FindInstrument отвечает по одному тикеру ~20 КБ.
  InstrumentsService/GetConsensusForecasts — консенсус-прогнозы по всем
      инструментам (постранично, страницы по 200 записей):
        instrumentUid              — ключ соответствия с UID из FindInstrument
        bestTargetPrice            — средняя целевая цена
        bestTargetLow / High       — диапазон целей (min/max)
        totalBuy/Hold/SellRecommend — число аналитиков по каждой рекомендации
        consensus                  — RECOMMENDATION_BUY / HOLD / SELL
        prognosisDate              — дата прогноза

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
import http.client
import json
import os
import re
import ssl
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

# --- TLS ---------------------------------------------------------------
# На GitHub Actions (ubuntu-latest) в наборе CA нет корневого сертификата
# НУЦ Минцифры (Russian Trusted Root CA), а серверы Т-Банка отдают цепочку
# с этим самоподписанным корнем: urllib падает с CERTIFICATE_VERIFY_FAILED
# (self-signed certificate in certificate chain). Поэтому корневые
# сертификаты НУЦ встроены в скрипт ниже и подключаются к стандартной
# строгой проверке. Если строгая проверка всё равно не проходит — резервное
# соединение без проверки (с пометкой в лог), чтобы дневное обновление
# не ломалось.
_EXTRA_CA_PEM = """\
-----BEGIN CERTIFICATE-----
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
-----BEGIN CERTIFICATE-----
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


def _tls_contexts():
    """Контексты TLS по убыванию строгости:
    1) системные CA + корневые сертификаты НУЦ (Russian Trusted Root/Sub CA);
    2) без проверки сертификата — резервный режим."""
    ctxs = []
    try:
        c = ssl.create_default_context()
        c.load_verify_locations(cadata=_EXTRA_CA_PEM)
        ctxs.append(c)
    except Exception as e:
        print(f"ВНИМАНИЕ: не удалось подключить сертификаты НУЦ ({e})")
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    ctxs.append(c)
    return ctxs


_TLS_CTXS = _tls_contexts()
_TLS_MODE = [None]


def _note(idx):
    """Однократно пометить в логе, какой режим TLS реально работает."""
    if _TLS_MODE[0] != idx:
        _TLS_MODE[0] = idx
        print("TLS: строгая проверка сертификата (системные CA + НУЦ)"
              if idx == 0 else
              "TLS: строгая проверка не прошла — соединение БЕЗ "
              "проверки сертификата (резервный режим)")


def _open(req, timeout):
    """urlopen c TLS-фолбэком: сначала строгая проверка сертификата
    (системные CA + НУЦ), при неудаче — соединение без проверки."""
    last = None
    for idx, ctx in enumerate(_TLS_CTXS):
        try:
            resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
            _note(idx)
            return resp
        except urllib.error.HTTPError as e:
            _note(idx)  # HTTP-ответ получен — TLS этого контекста сработал
            raise       # статусы API (401/429/5xx) обрабатываются в call()
        except (urllib.error.URLError, ssl.SSLError, TimeoutError, OSError) as e:
            last = e
    raise last


def call(method, payload, tries=3, token=None):
    """POST к REST-шлюзу T-Invest API c ретраем на 429/5xx (как в fetch_tbank_portfolio)."""
    tk = token if token is not None else TOKEN
    if not tk:
        sys.exit("Ошибка: не задан токен. export TB_TOKEN='t.xxx' "
                 "(read-only токен T-Invest API, тот же, что для выгрузки портфеля).")
    data = json.dumps(payload).encode("utf-8")
    for attempt in range(1, tries + 1):
        req = urllib.request.Request(
            # Шлюз T-Invest REST: имя сервиса отделяется ТОЧКОЙ
            # (…contract.v1.InstrumentsService/Shares) — через слэш он даёт 404.
            f"{BASE}.{method}",
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
            with _open(req, 30) as resp:
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
        except (urllib.error.URLError, TimeoutError, OSError,
                http.client.HTTPException):
            # http.client.HTTPException покрывает IncompleteRead: ответ может
            # оборваться на середине (в т.ч. постраничный консенсус) — повтор.
            if attempt < tries:
                time.sleep(5 * attempt)
                continue
            raise


def dec(q):
    """Quotation (units + nano) -> float; units приходит строкой."""
    if not q:
        return None
    try:
        return round(int(q.get("units") or 0) + int(q.get("nano") or 0) / 1e9, 6)
    except (TypeError, ValueError):
        return None


def is_isin(t):
    """Облигации (ISIN вроде RU000A10C5L7) — консенсуса по ним нет."""
    return re.match(r"^RU[0-9A-Z]{10}$", t) is not None


def ticker_to_uid(stocks, token=None):
    """Тикер -> UID инструмента через FindInstrument по каждому тикеру.

    Раньше карта строилась одним запросом InstrumentsService/Shares, но его
    ответ ~3 МБ и на GitHub Actions обрывался (IncompleteRead). Здесь ответ
    на один тикер ~20 КБ; при сбое конкретный запрос повторяется в call().
    Приоритет — основному борду TQBR, если тикер встречается в нескольких
    секциях."""
    t2u = {}
    for s in stocks:
        t = s["ticker"]
        try:
            r = call("InstrumentsService/FindInstrument",
                     {"query": t}, token=token).get("instruments", [])
        except Exception as e:
            print(f"  {t}: поиск UID не удался — {e}")
            continue
        hits = [i for i in r if i.get("ticker") == t]
        hit = next((i for i in hits if i.get("classCode") == "TQBR"),
                   hits[0] if hits else None)
        if hit and hit.get("uid"):
            t2u[t] = (hit["uid"], hit.get("classCode"))
        else:
            print(f"  {t}: тикер не найден в API — консенсус недоступен")
        time.sleep(0.4)
    return t2u


def load_consensus(token=None):
    """UID -> запись консенсуса (постранично). Останов: пустая страница или всё собрано.
    Сбой одной страницы не теряет уже собранные: если прочитать удалось хоть
    одну — работаем с частичными данными (пометка в лог); если ни одну —
    исключение уходит наверх (прогнозы остаются прежними).
    Ключ записи берём defensively (instrumentUid/uid), имя массива тоже
    (items/instruments/...) — схема метода в доках портала не отдаётся, а
    вернуть её дословно может первый сырой ответ (второй элемент результата).
    Возвращает (items, raw_first)."""
    items, page, ok, pages, seen = {}, 0, 0, 0, 0
    total = None
    raw_first = None
    while True:
        try:
            resp = call("InstrumentsService/GetConsensusForecasts",
                        {"paging": {"limit": 200, "pageNumber": page}}, token=token)
        except Exception as e:
            if ok == 0:
                raise
            print(f"ВНИМАНИЕ: страница консенсуса №{page} не прочитана ({e}) — "
                  f"работаем с частичными данными ({len(items)} записей)")
            break
        if raw_first is None:
            raw_first = resp
        chunk = (resp.get("items") or resp.get("instruments")
                 or resp.get("consensusForecasts") or resp.get("forecasts") or [])
        got_key = 0
        for it in chunk:
            if not isinstance(it, dict):
                continue
            key = (it.get("instrumentUid") or it.get("uid")
                   or it.get("instrumentUID") or it.get("instrument_id"))
            got_key += 1 if key else 0
            items[key] = it
        seen += sum(1 for it in chunk if isinstance(it, dict))
        pr = resp.get("page") or {}
        total = pr.get("totalCount") if pr.get("totalCount") is not None else total
        page += 1
        ok += 1
        pages = ok
        if not chunk or (total is not None and seen >= total) or page > 60:
            break
        if got_key == 0:
            # записи пришли, но ни в одной нет опознанного поля UID —
            # дальнейшие страницы бесполезны, смотрим ДИАГНОСТИКУ в main()
            break
        time.sleep(0.2)
    print(f"консенсус: {seen} записей за {pages} страниц(ы)")
    return items, raw_first


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
        items, raw_first = load_consensus()
    except Exception as e:
        print(f"ВНИМАНИЕ: консенсус не загружен ({e}) — прогнозы оставлены прежними; "
              f"дневное обновление продолжается")
        return

    updated, moved, empty = [], [], []
    for s in targets:
        t = s["ticker"]
        pair = t2u.get(t)
        item = items.get(pair[0]) if pair and pair[0] else None
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
    if not updated and items:
        k0, it0 = next(iter(items.items()))
        uid0 = (next(iter(t2u.values()))[0] if t2u else "нет")
        print("ДИАГНОСТИКА: записи консенсуса есть, но ни один UID не совпал —")
        print("  пример UID из FindInstrument:", str(uid0))
        print("  пример ключа записи консенсуса:", str(k0))
        print("  поля записи:", ", ".join(sorted(it0.keys())))
        print("  пример записи:", json.dumps(it0, ensure_ascii=False)[:400])
    elif not updated and not items and raw_first is not None:
        print("ДИАГНОСТИКА: 0 записей — имя массива/paging может отличаться; "
              "сырой ответ первой страницы:")
        print(" ", json.dumps(raw_first, ensure_ascii=False)[:600])
    print("обновлено прогнозов:", len(updated), "| цели изменились:", len(moved),
          "| без консенсуса:", ", ".join(empty) or "нет")
    for x in moved:
        print("  цель сменилась:", x)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

if __name__ == "__main__":
    main()
