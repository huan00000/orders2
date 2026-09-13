import hmac
import os
import sys
from time import time
import hashlib

# =========================== 配置区 ===========================
HOST = "https://api.gateio.ws"
PREFIX = "/api/v4"
# =============================================================

# ========== 从 .env 加载 API 密钥 ==========
def load_env():
    """读取同目录下的 .env 文件，返回 key-value 字典"""
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    env_vars = {}
    if os.path.exists(env_path):
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    key, value = line.split('=', 1)
                    env_vars[key.strip()] = value.strip().strip('"\'')
    # Docker Compose 注入的环境变量优先于本地文件。
    env_vars.update(os.environ)
    return env_vars

env_data = load_env()
API_KEY = env_data.get('API_KEY')
API_SECRET = env_data.get('API_SECRET')

if not API_KEY or not API_SECRET:
    print("错误: 环境变量或 .env 中必须包含 API_KEY 和 API_SECRET")
    sys.exit(1)

def gen_sign(method, url, query_string=None, payload_string=None):
    """生成 Gate.io API 签名头"""
    t = time()
    m = hashlib.sha512()
    m.update((payload_string or "").encode('utf-8'))
    hashed_payload = m.hexdigest()
    s = '%s\n%s\n%s\n%s\n%s' % (method, url, query_string or "", hashed_payload, t)
    sign = hmac.new(API_SECRET.encode('utf-8'), s.encode('utf-8'), hashlib.sha512).hexdigest()
    return {'KEY': API_KEY, 'Timestamp': str(t), 'SIGN': sign}