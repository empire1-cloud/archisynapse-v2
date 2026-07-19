FROM node:22-alpine AS builder

WORKDIR /app

COPY package*.json ./
COPY services/ledger/package*.json ./services/ledger/

RUN npm ci --workspace=services/ledger

COPY services/ledger/ ./services/ledger/

RUN npm run build --workspace=services/ledger

FROM node:22-alpine AS runner

WORKDIR /app

COPY package*.json ./
COPY services/ledger/package*.json ./services/ledger/

RUN npm ci --omit=dev --workspace=services/ledger

COPY --from=builder /app/services/ledger/dist ./services/ledger/dist

WORKDIR /app/services/ledger

EXPOSE 3001

CMD ["node", "dist/index.js"]
