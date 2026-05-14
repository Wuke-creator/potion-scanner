FROM node:22-alpine AS build
WORKDIR /app
COPY trade-executor/package.json trade-executor/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY trade-executor/tsconfig.json ./
COPY trade-executor/src ./src
RUN npm run build

FROM node:22-alpine AS runtime
WORKDIR /app
ENV NODE_ENV=production
COPY trade-executor/package.json trade-executor/package-lock.json* ./
RUN npm install --omit=dev --no-audit --no-fund
COPY --from=build /app/dist ./dist
EXPOSE 3001
CMD ["node", "dist/index.js"]
