FROM node:22-bookworm-slim AS builder
WORKDIR /build
RUN npm install --global pnpm@11.25.0
COPY web/package.json web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY web/index.html web/tsconfig.json web/vite.config.ts ./
COPY web/src ./src
RUN pnpm run build

FROM nginx:stable-alpine
COPY web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /build/dist /usr/share/nginx/html
EXPOSE 8501
