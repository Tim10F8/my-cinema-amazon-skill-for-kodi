FROM python:3.9-slim

WORKDIR /app

# Installation des outils système (ADB + Ping)
RUN apt-get update && apt-get install -y \
    android-tools-adb \
    iputils-ping \
    && rm -rf /var/lib/apt/lists/*

# Installation des dépendances Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "app.py"]
