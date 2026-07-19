FROM node:22-alpine AS builder

WORKDIR /app

COPY package*.json ./
COPY services/transaction/package*.json ./services/transaction/

RUN npm ci --workspace=services/transaction

COPY services/transaction/ ./services/transaction/

RUN npm run build --workspace=services/transaction

FROM node:22-alpine AS runner

WORKDIR /app

COPY package*.json ./
COPY services/transaction/package*.json ./services/transaction/

RUN npm ci --omit=dev --workspace=services/transaction

COPY --from=builder /app/services/transaction/dist ./services/transaction/dist

WORKDIR /app/services/transaction

EXPOSE 3000

CMD ["node", "dist/index.js"]
