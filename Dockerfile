FROM python:3.10-slim
WORKDIR /app
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive \
    PADDLE_DEVICE=cpu
RUN mkdir -p /etc/paddleocr
CMD ["bash", "start.sh"]
EXPOSE 8080

COPY . /app
RUN bash install.sh
