# IFU compute back-end (Flask + cadquery/OCCT) for Render.
# cadquery-ocp ships prebuilt OpenCascade Linux wheels, so we DON'T
# compile OCCT -- we just need its runtime shared libs (OpenGL/X11/
# fontconfig) present in the image.
FROM python:3.12-slim

# Runtime shared libs that the OCCT wheels dlopen at import/render time.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglu1-mesa \
        libxrender1 \
        libxext6 \
        libsm6 \
        libice6 \
        libx11-6 \
        libfontconfig1 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code (see .dockerignore for what's excluded).
COPY . .

# Persistent state lives on the mounted disk (see render.yaml), NOT in
# the image layer (which is ephemeral on redeploy).
ENV IFU_DATA_DIR=/data \
    PYTHONUNBUFFERED=1
RUN mkdir -p /data

# Render injects $PORT; gunicorn.conf.py binds to it (default 10000).
EXPOSE 10000
CMD ["gunicorn", "-c", "gunicorn.conf.py", "wsgi:app"]
