import requests
from general import gen_sign

host = "https://api.gateio.ws"
prefix = "/api/v4"
headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}

def get_cross_available(session):
    """获取统一账户的可用保证金总额"""
    url = '/futures/usdt/accounts'
    headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
    sign_headers = gen_sign('GET', prefix + url, '')
    headers.update(sign_headers)
    resp = session.get(host + prefix + url, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    cross_available = float(data.get('cross_available', 0.0))
    print(f"cross_available: {cross_available}")
    return cross_available

if __name__ == "__main__":
    with requests.Session() as session:
        available = get_cross_available(session)
        print(f"统一账户可用保证金总额: {available} USDT")
