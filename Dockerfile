FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py request_log.py general.py available.py getorder.py pto.py checkstatus.py ./
COPY container_entrypoint.py orderlist.example.js ./
CMD ["python", "container_entrypoint.py"]
