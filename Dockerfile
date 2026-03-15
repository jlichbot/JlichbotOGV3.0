FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Install sitecustomize so price_fallback auto-patches every process
RUN cp sitecustomize.py $(python -c "import site; print(site.getsitepackages()[0])")/sitecustomize.py

# Writable dir for daily_spend.json
RUN mkdir -p /data && chmod 777 /data

CMD ["python", "run.py"]
