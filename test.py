# coding: utf-8
import requests
import time
import hashlib
import hmac
from general import gen_sign

host = "https://api.gateio.ws"
prefix = "/api/v4"
headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}

url = '/futures/usdt/autoorder/v1/trail/detail'
query_param = 'id=1104705'
# `gen_sign` 的实现参考认证一章
if __name__ == "__main__":
    sign_headers = gen_sign('GET', prefix + url, query_param)
    headers.update(sign_headers)
    r = requests.request('GET', host + prefix + url + "?" + query_param, headers=headers)
    print(r.json())
