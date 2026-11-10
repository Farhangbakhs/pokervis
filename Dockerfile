# The public site: game server, card reader and Solana chain service in one container.
# Tables live in memory and the chain service keeps its ledger on one volume, so run exactly ONE instance.
FROM node:22-slim AS chain
WORKDIR /chain
COPY chain/service/package.json chain/service/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY chain/service/src src
COPY chain/service/tsconfig.json .

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOSTED=1 \
    TORCH_HOME=/app/.torch YOLO_CONFIG_DIR=/tmp/ultralytics YOLO_AUTOINSTALL=false YOLO_OFFLINE=true

RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
COPY --from=chain /usr/local/bin/node /usr/local/bin/node

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir -r requirements.txt
# the Queen fix's ResNet weights, baked in so the first table does not wait on a download
RUN python -c "import torchvision as t; t.models.resnet18(weights=t.models.ResNet18_Weights.IMAGENET1K_V1)"

COPY game game
COPY server server
COPY vision vision
COPY web web
COPY models models
COPY --from=chain /chain chain/service
# public data only (mint, token accounts, past signatures): the ledger's starting point on a new volume
COPY chain/service/.state/state.json chain-seed/state.json
COPY scripts/hosted.sh hosted.sh

EXPOSE 8000
CMD ["bash", "hosted.sh"]
