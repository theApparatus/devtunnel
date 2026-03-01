FROM alpine:3.20

ARG FRP_VERSION=0.67.0

RUN apk add --no-cache ca-certificates tzdata curl \
    && curl -fSL "https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/frp_${FRP_VERSION}_linux_amd64.tar.gz" \
       -o /tmp/frp.tar.gz \
    && tar -xzf /tmp/frp.tar.gz -C /tmp \
    && mv /tmp/frp_${FRP_VERSION}_linux_amd64/frps /usr/local/bin/frps \
    && chmod +x /usr/local/bin/frps \
    && rm -rf /tmp/frp* \
    && apk del curl

COPY frps.toml /etc/frp/frps.toml

EXPOSE 8080

ENTRYPOINT ["frps", "-c", "/etc/frp/frps.toml"]
