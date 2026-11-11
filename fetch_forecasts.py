#!/usr/bin/env python3
"""Ежедневное обновление сводных прогнозов Т-Банка (консенсус аналитиков).

Источник: страница бумаги tbank.ru/invest/stocks/<ТИКЕР>/ — в HTML зашит блок
"consensus" (рекомендация, средняя целевая цена, min/max целей) и список
отдельных прогнозов аналитиков. Скрипт обновляет в portfolio_data.json по
каждой акции: recommendation, target_price, range_min/range_max, n_forecasts
(число прогнозов в консенсусе), upside_pct (по текущей цене из JSON) и
summary.recommendations. Дата выгрузки пишется в meta.forecasts_date и
meta.sources.forecasts — её подхватывают make_excel.py и страницы.

Бумаги без консенсуса (сейчас FEES, PMSB) и облигации (ISIN) не трогаются —
прежние значения и ручные примечания остаются как есть. Скрипт не прерывает
ежедневный конвейер: если страницу Т-Банка не удалось загрузить, прогноз
остаётся прежним, ошибка печатается в журнал.

Запускается GitHub Actions (workflow daily-update, шаг ПЕРЕД auto_update.py,
чтобы свежие цели попали в пересчёт цен/потенциала) или вручную:
    python fetch_forecasts.py
"""
import json, os, re, ssl, time, datetime, urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
CTX = ssl.create_default_context(); CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36'}
SKIP = {'PINS-RM'}                          # бумага без консенсуса, как и в auto_update.py
REC_BUCKET = {'Покупать': 'buy', 'Держать': 'hold', 'Продавать': 'sell'}

def get(url):
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=45, context=CTX).read().decode('utf-8', 'ignore')

def is_isin(t):
    """Облигации (ISIN вроде RU000A10C5L7) — страницы /stocks/ нет, прогноза нет."""
    return re.match(r'^RU[0-9A-Z]{10}$', t) is not None

def extract_consensus(html, ticker):
    """Достаёт блок "consensus":{...} по тикеру (скобки балансом, без регэкспа внутрь)."""
    key = '"consensus":{'
    for m in re.finditer(re.escape(key), html):
        i = m.end() - 1                     # позиция открывающей скобки
        depth = 0
        for j in range(i, min(i + 4000, len(html))):
            if html[j] == '{': depth += 1
            elif html[j] == '}':
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(html[i:j + 1])
                    except Exception:
                        obj = None
                    if obj and obj.get('ticker') == ticker:
                        return obj
                    break                   # чужой блок — смотрим следующий
    return None

def fetch_consensus(ticker):
    """(consensus|None, число отдельных прогнозов аналитиков)."""
    h = get(f'https://www.tbank.ru/invest/stocks/{ticker}/')
    return extract_consensus(h, ticker), len(re.findall(r'\{"ticker":"' + re.escape(ticker) + r'","analyst":"', h))

def main():
    path = os.path.join(BASE, 'portfolio_data.json')
    d = json.load(open(path, encoding='utf-8'))
    today = datetime.date.today()
    updated, moved, failed, empty = [], [], [], []

    for s in d['stocks']:
        t = s['ticker']
        if t in SKIP or is_isin(t):
            continue
        try:
            time.sleep(0.3)                 # вежливая пауза между запросами
            c, n = fetch_consensus(t)
        except Exception as e:
            failed.append(t)
            print(f'  {t}: страница не загрузилась — {e}; прогноз оставлен прежним')
            continue
        if not c or c.get('consensus') is None:
            empty.append(t)
            print(f'  {t}: сводного прогноза в Т-Банке нет (прогнозов аналитиков: {n}) — оставляем прежнее')
            continue
        old = s.get('target_price')
        s['recommendation'] = c.get('recommendation')
        s['target_price'] = c.get('consensus')
        s['range_min'] = c.get('minTarget')
        s['range_max'] = c.get('maxTarget')
        s['n_forecasts'] = n
        price = s.get('current_price')
        s['upside_pct'] = (round((s['target_price'] / price - 1) * 100, 2)
                           if (s['target_price'] and price) else None)
        updated.append(t)
        extra = '' if old == s['target_price'] else f'  (было {old})'
        if old != s['target_price']:
            moved.append(f'{t}: {old} → {s["target_price"]}')
        print(f"  {t}: {s['recommendation']}, цель {s['target_price']} ₽ "
              f"[{s['range_min']} — {s['range_max']}], аналитиков {n}{extra}")

    # счётчик рекомендаций (его показывают Excel и страницы)
    recs = {'buy': 0, 'hold': 0, 'sell': 0, 'no_forecast': 0}
    for s in d['stocks']:
        recs[REC_BUCKET.get(s.get('recommendation'), 'no_forecast')] += 1
    d['summary']['recommendations'] = recs
    if updated:
        d['meta']['forecasts_date'] = str(today)
        d['meta']['sources']['forecasts'] = ('tbank.ru/invest — сводный прогноз на '
            + today.strftime('%d.%m.%Y') + ' (автообновление GitHub Actions)')

    json.dump(d, open(path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('обновлено прогнозов:', len(updated), '| цели изменились:', len(moved),
          '| без прогноза в Т-Банке:', ', '.join(empty) or 'нет',
          '| ошибок загрузки:', ', '.join(failed) or 'нет')
    for x in moved:
        print('  цель сменилась:', x)

if __name__ == '__main__':
    main()
