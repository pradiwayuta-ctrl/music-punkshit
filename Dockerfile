FROM python:3.10-slim

# Install system dependencies yang dibutuhkan PyTgCalls/Pyrogram
RUN apt-get update && apt-get install -y \
    ffmpeg \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Salin requirements jika ada, atau install langsung
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Salin seluruh kode proyek ke dalam direktori /app container
COPY . .

CMD ["python", "main.py"]
