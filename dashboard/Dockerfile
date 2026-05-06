# Multi-stage Next.js build for Railway.
#
# Used by the potion-dashboard Railway service. The bot service still
# uses the repo-root Dockerfile (Python). Two services on the same repo,
# two different Dockerfiles, selected per-service via the
# RAILWAY_DOCKERFILE_PATH env var.

# ---- Stage 1: build ----
FROM node:20-alpine AS builder
WORKDIR /app

# Build context on Railway is the REPO ROOT (not dashboard/), even
# with RAILWAY_DOCKERFILE_PATH=dashboard/Dockerfile. So all COPY paths
# below have to be prefixed with `dashboard/` to scope correctly.

# Copy lockfile + package.json for layer-cached deps install.
COPY dashboard/package.json dashboard/package-lock.json* ./
RUN npm ci --no-audit --no-fund

# Copy the rest of the dashboard source + build.
COPY dashboard/ ./
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

# ---- Stage 2: runtime ----
FROM node:20-alpine AS runtime
WORKDIR /app

# Non-root user for the Next.js process. Railway mounts volumes as
# root-owned by default, so if the dashboard ever needs WRITE access
# to /app/data we'd flip back to running as root. Read-only SQLite
# access works fine as the nextjs user.
RUN addgroup --system --gid 1001 nodejs \
 && adduser --system --uid 1001 nextjs

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=3000

# Copy only what's needed for runtime: built output + node_modules + package.json.
COPY --from=builder --chown=nextjs:nodejs /app/.next ./.next
COPY --from=builder --chown=nextjs:nodejs /app/node_modules ./node_modules
COPY --from=builder --chown=nextjs:nodejs /app/package.json ./package.json
COPY --from=builder --chown=nextjs:nodejs /app/next.config.ts ./next.config.ts
# Static assets served at the site root (banner images referenced from
# outbound emails, plus anything else dropped under dashboard/public/).
COPY --from=builder --chown=nextjs:nodejs /app/public ./public

USER nextjs

EXPOSE 3000
CMD ["npm", "start"]
