"""首次部署必须显式准备订单状态，避免更新镜像时重置状态。"""
import os
import sys
from pathlib import Path

if __name__ == "__main__":
    data = Path(os.environ.get("DATA_DIR", "/data"))
    if not (data / "orderlist.js").is_file():
        sys.exit("缺少 /data/orderlist.js：请按 README 迁移现有状态或初始化新实例。")
    os.execv(sys.executable, [sys.executable, "/app/main.py", *sys.argv[1:]])
