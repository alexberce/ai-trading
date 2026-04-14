FROM node:22-slim

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm ci

COPY tsconfig.json ./
COPY src/ ./src/

RUN npx tsc

EXPOSE 8080

CMD ["node", "dist/index.js"]
