import json
import urllib.request
from pathlib import Path

symbols = ['BTC-USDT-SWAP','ETH-USDT-SWAP','SOL-USDT-SWAP','XRP-USDT-SWAP','CRV-USDT-SWAP']
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    'Accept': 'application/json,text/plain,*/*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Origin': 'https://www.okx.com',
    'Referer': 'https://www.okx.com/',
}

def fetch(url):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode('utf-8'))

payload = {'generated_at': None, 'symbols': {}}
try:
    payload['generated_at'] = fetch('https://www.okx.com/api/v5/public/time')['data'][0]['ts']
except Exception:
    payload['generated_at'] = ''

for symbol in symbols:
    try:
        ticker = fetch(f'https://www.okx.com/api/v5/market/ticker?instId={symbol}')
        inst = fetch(f'https://www.okx.com/api/v5/public/instruments?instType=SWAP&instId={symbol}')
        t = ticker.get('data', [{}])[0]
        i = inst.get('data', [{}])[0]
        last = float(t.get('last') or 0)
        ct_val = float(i.get('ctVal') or 0)
        ct_ccy = str(i.get('ctValCcy') or '').upper()
        contract_value = ct_val if ct_ccy == 'USDT' else ct_val * last
        payload['symbols'][symbol] = {
            'price': last,
            'price_source': 'server_okx',
            'ct_val': ct_val,
            'ct_val_ccy': ct_ccy,
            'contract_value_usdt': contract_value,
            'inst_family': i.get('instFamily',''),
            'inst_id': symbol,
        }
    except Exception as e:
        payload['symbols'][symbol] = {'error': str(e)}

out = Path('/opt/okx_quant/data/okx_market.live.json')
out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
print(out)
