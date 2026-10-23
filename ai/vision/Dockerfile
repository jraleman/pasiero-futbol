# matchvision: match video → the plays it finds, and the Video tab's boxes, on CPU.
# For an NVIDIA GPU, start from a CUDA image and install torch's CUDA build instead.
FROM python:3.12-slim

# ffmpeg: decoding what OpenCV can't (AV1, some WebM) and writing H.264;
# libgl1, libglib2.0-0: the full OpenCV that Ultralytics depends on
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.14.1 torchvision==0.29.1 \
    && pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /app
COPY fetch_models.py ./
COPY matchvision ./matchvision

# Models are mounted (vision/models → /models); fetch them with fetch_models.py
ENV MODELS_DIR=/models \
    YOLO_CONFIG_DIR=/tmp/ultralytics \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN python -c "import ultralytics, cv2, torch; from matchvision import analyze, check"

ENTRYPOINT ["python", "-m", "matchvision.analyze"]
