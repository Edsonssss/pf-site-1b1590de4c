#!/usr/bin/env python3
"""Ежедневное автообновление данных портфеля (запускается GitHub Actions или вручную).
Обновляет: цены акций (MOEX, интрадей/закрытие), паи и доходности фондов (investfunds),
статусы дивидендов (отсечка прошла -> выплачен), итоги и доли.
Количества бумаг и целевые прогнозы меняются только вручную (через чат).
После данных: пересобирает portfolio_data.js, пере-встраивает JSON в portfolio.html,
и (если задан DRIVE_SA_KEY) обновляет файлы в Google Drive.
"""
import json, os, re, ssl, datetime, urllib.request
import jwt

BASE = os.path.dirname(os.path.abspath(__file__))
CTX = ssl.create_default_context(); CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36'}

def get(url):
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=40, context=CTX).read().decode('utf-8', 'ignore')

def p(path): return os.path.join(BASE, path)

today = datetime.date.today()
d = json.load(open(p('portfolio_data.json'), encoding='utf-8'))
div = json.load(open(p('dividends_raw.json'), encoding='utf-8'))

# ---------- цены акций: MOEX ISS ----------
tickers = [s['ticker'] for s in d['stocks'] if s['ticker'] not in ('PINS-RM',)]
url = ('https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities.json?securities='
       + ','.join(tickers) + '&iss.meta=off')
j = json.loads(get(url))
sc, sd = j['securities']['columns'], j['securities']['data']
iS, iP, iD = sc.index('SECID'), sc.index('PREVPRICE'), sc.index('PREVDATE')
mc, md = j['marketdata']['columns'], j['marketdata']['data']
mS, mL = mc.index('SECID'), mc.index('LAST')
moex = {}
for r in sd:
    if r[iS] in tickers: moex[r[iS]] = {'prev': r[iP], 'prevdate': r[iD]}
for r in md:
    if r[mS] in tickers: moex.setdefault(r[mS], {})['last'] = r[mL]
if len(moex) < len(tickers) - 2:
    raise SystemExit('MOEX отдал слишком мало бумаг — пропуск обновления')

# ---------- дивиденды за 12 мес ----------
t12 = {}
for t, arr in div.items():
    s = 0.0
    for x in arr:
        try: dt = datetime.date.fromisoformat(x['reestr'])
        except Exception: continue
        if today - datetime.timedelta(days=365) <= dt <= today: s += x['dividend']['value']
    t12[t] = s

changed = []
for s in d['stocks']:
    t = s['ticker']
    if t not in moex or moex[t].get('last') in (None,): continue
    m = moex[t]
    if s['current_price'] != m['last']: changed.append(t)
    s['current_price'] = m['last']; s['prev_close'] = m['prev']; s['prev_date'] = m['prevdate']
    s['day_change_pct'] = round((m['last'] / m['prev'] - 1) * 100, 2)
    s['value'] = round(s['qty'] * m['last'], 2)
    if s.get('target_price'): s['upside_pct'] = round((s['target_price'] / m['last'] - 1) * 100, 2)
    s['div_yield_12m_pct'] = round(t12.get(t, 0) / m['last'] * 100, 1) if t12.get(t) else 0.0

# ---------- статусы дивидендов: отсечка прошла -> выплачен ----------
for u in list(d['upcoming_dividends']):
    rd = datetime.date.fromisoformat(u['record_date'])
    if rd < today:
        for s in d['stocks']:
            if s['ticker'] == u['ticker']:
                s['dividend_next'] = None
                s['dividend_date'] = f"выплачен {rd.strftime('%d.%m.%Y')} ({u['dividend']} ₽)"
                s['dividend_status'] = 'выплачен'
        d['upcoming_dividends'].remove(u)
        changed.append(u['ticker'] + ':div')

# ---------- фонды: investfunds ----------
FUND_URL = {'ОПИФ «Флагманский»': 'https://investfunds.ru/funds/10353/',
            'ОПИФ «Рублевые перспективы»': 'https://investfunds.ru/funds/11073/',
            'БПИФ «БКС Денежный рынок» (BCSD)': 'https://investfunds.ru/funds/10831/',
            'БПИФ «Т-Капитал Денежный рынок» (TMON)': 'https://investfunds.ru/funds/8628/'}
def fund_nav(u):
    t = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', re.sub(r'<script[\s\S]*?</script>', '', get(u))))
    m = re.search(r'и СЧА (\d\d\.\d\d\.\d{4})\s+Пай\s+([\d.]+(?:\s\d[\d.]*)?)\s+СЧА', t)
    pm = re.search(r'1 мес\s+3 мес\s+6 мес\s+1 год\s+3 года\s+5 лет\s+([-\d.,%—]+(?:\s+[-\d.,%—]+){6})', t)
    perf = None
    if pm:
        tk = pm.group(1).split(); f = lambda x: None if x == '—' else float(x.replace('%', ''))
        perf = {'m1': f(tk[1]), 'm3': f(tk[2]), 'm6': f(tk[3]), 'y1': f(tk[4])}
    return (m.group(1), float(m.group(2).replace(' ', '')), perf) if m else (None, None, None)
for f in d['funds']:
    u = FUND_URL.get(f['name'])
    if u:
        try:
            dts, nav, perf = fund_nav(u)
        except Exception:
            dts = nav = perf = None
        if nav and (f['current_nav'] != nav or f.get('nav_date') != dts):
            f['current_nav'] = nav; f['nav_date'] = dts
            if perf: f['performance_pct'] = perf
            changed.append(f['name'][:12])
    # стоимость пересчитываем всегда — qty мог измениться вручную при том же NAV
    f['value'] = round(f['qty'] * f['current_nav'], 2)

# ---------- итоги ----------
sm = d['summary']
sm['stocks_value'] = round(sum(s['value'] for s in d['stocks']), 2)
sm['funds_value'] = round(sum(f['value'] for f in d['funds']), 2)
sm['total_value'] = round(sm['stocks_value'] + sm['funds_value'] + sm['cash_value'], 2)
sm['shares_pct'] = {'stocks': round(sm['stocks_value'] / sm['total_value'] * 100, 2),
                    'funds': round(sm['funds_value'] / sm['total_value'] * 100, 2),
                    'cash': round(sm['cash_value'] / sm['total_value'] * 100, 2)}
num = [s for s in d['stocks'] if s.get('upside_pct') is not None]
sm['weighted_upside_pct'] = round(sum(s['value'] * s['upside_pct'] for s in num) / sum(s['value'] for s in num), 1)
sm['dividends_12m_gross'] = round(sum(t12.get(s['ticker'], 0) * s['qty'] for s in d['stocks']), 2)
sm['dividends_12m_yield_pct'] = round(sm['dividends_12m_gross'] / sm['total_value'] * 100, 2)
sm['upcoming_dividends_gross'] = round(sum(u['gross'] for u in d['upcoming_dividends']), 2)
sm['upcoming_dividends_net_13pct'] = round(sm['upcoming_dividends_gross'] * 0.87, 2)
sm['blocked_value_rub'] = round(sum(b['value_rub'] for b in d.get('blocked', [])), 2)
sm['total_including_blocked'] = round(sm['total_value'] + sm['blocked_value_rub'], 2)
fam = d.get('family') or {}
if fam:
    sm['family_value'] = fam.get('value', 0)
    sm['total_family'] = round(sm['total_value'] + sm['family_value'], 2)
d['meta']['date'] = str(today)
pd = sorted({s.get('prev_date') for s in d['stocks'] if s.get('prev_date')})[-1]
d['meta']['sources']['prices'] = (f'MOEX ISS: текущие цены — {today.strftime("%d.%m.%Y")} (интрадей), '
                                  f'цены закрытия — {pd[8:10]}.{pd[5:7]}.{pd[:4]}; автообновление GitHub Actions')

# ---------- история ежедневных остатков (history.json) ----------
hist_path = p('history.json')
try:
    hist = json.load(open(hist_path, encoding='utf-8'))
    if not isinstance(hist.get('days'), list): hist['days'] = []
except Exception:
    hist = {'title': 'Учёт ежедневных остатков — акции, фонды, счета, сэйв жены', 'currency': 'RUB', 'days': []}
rec = {'date': str(today), 'stocks': sm['stocks_value'], 'funds': sm['funds_value'],
       'cash': sm['cash_value'], 'save_wife': sm.get('family_value', 0),
       'blocked_rub': sm.get('blocked_value_rub', 0), 'total': sm['total_value'],
       'total_incl_blocked': sm.get('total_including_blocked', sm['total_value']),
       'total_family': sm.get('total_family', sm['total_value']),
       'source': 'автообновление GitHub Actions' if os.environ.get('GITHUB_ACTIONS') else 'автообновление'}
hist['days'] = sorted([x for x in hist['days'] if x.get('date') != str(today)] + [rec],
                      key=lambda x: x['date'])
hist['updated'] = str(today)
json.dump(hist, open(hist_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
d['history'] = hist['days'][-90:]   # копия последних 90 дней — для офлайн-графика на странице

json.dump(d, open(p('portfolio_data.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

# ---------- portfolio_data.js ----------
js = json.dumps(d, ensure_ascii=False).replace('</', '<\\/')
open(p('portfolio_data.js'), 'w', encoding='utf-8').write(
    '// Сгенерировано из portfolio_data.json\nwindow.PORTFOLIO_DATA = ' + js + ';\n')

# ---------- embed в html-страницу (index.html в репо / portfolio.html локально) ----------
HTML = 'index.html' if os.path.exists(p('index.html')) else 'portfolio.html'
h = open(p(HTML), encoding='utf-8').read()
emb = json.dumps(d, ensure_ascii=False).replace('</', '<\\/')
h2, n = re.subn(r'(<script id="embedded-data" type="application/json">)[\s\S]*?(</script>)',
                lambda mo: mo.group(1) + emb + mo.group(2), h, count=1)
if n == 1: open(p(HTML), 'w', encoding='utf-8').write(h2)

print('изменено позиций:', len(changed), '| total:', sm['total_value'], '| дата:', d['meta']['date'])

# ---------- Google Drive (опционально) ----------
key_raw = os.environ.get('DRIVE_SA_KEY', '')
if key_raw:
    try:
        KEY = json.loads(key_raw)
        FOLDER = '0B-tnKJ9INY9WeVRsdndmWlM1Nzg'
        now = int(datetime.datetime.now().timestamp())
        claims = {'iss': KEY['client_email'], 'scope': 'https://www.googleapis.com/auth/drive',
                  'aud': KEY['token_uri'], 'exp': now + 3600, 'iat': now}
        t = jwt.encode(claims, KEY['private_key'], algorithm='RS256')
        resp = json.loads(urllib.request.urlopen(urllib.request.Request(
            KEY['token_uri'], data=('grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer&assertion=' + t).encode(),
            headers={'Content-Type': 'application/x-www-form-urlencoded'}), timeout=30, context=CTX).read())
        AT = resp['access_token']
        def find(name):
            q = urllib.parse.quote(f"name = '{name}' and '{FOLDER}' in parents and trashed = false") if 'urllib.parse' in dir() else None
            import urllib.parse as up
            q = up.quote(f"name = '{name}' and '{FOLDER}' in parents and trashed = false")
            r = json.loads(urllib.request.urlopen(urllib.request.Request(
                f'https://www.googleapis.com/drive/v3/files?q={q}&fields=files(id)',
                headers={'Authorization': 'Bearer ' + AT}), timeout=30, context=CTX).read())
            return r['files'][0]['id'] if r['files'] else None
        for name, fn in [('portfolio.html', HTML), ('portfolio_data.json', 'portfolio_data.json'), ('portfolio_data.js', 'portfolio_data.js'), ('history.json', 'history.json')]:
            fid = find(name)
            if fid:
                req = urllib.request.Request(f'https://www.googleapis.com/upload/drive/v3/files/{fid}?uploadType=media',
                    data=open(p(fn), 'rb').read(), method='PATCH',
                    headers={'Authorization': 'Bearer ' + AT, 'Content-Type': 'application/octet-stream'})
                urllib.request.urlopen(req, timeout=60, context=CTX)
        print('Drive: файлы обновлены')
    except Exception as e:
        print('Drive sync error:', e)
