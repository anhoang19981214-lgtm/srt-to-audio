FROM python:3.10-slim

# Cài ffmpeg (có quyền root trong Docker)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Cài Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy toàn bộ code
COPY . .

# Tạo thư mục uploads/outputs
RUN mkdir -p uploads outputs

# Expose port
EXPOSE $PORT

# Khởi động server
CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120
