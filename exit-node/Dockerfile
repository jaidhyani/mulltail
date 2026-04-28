FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    wireguard-tools openresolv curl ca-certificates iproute2 iptables jq procps \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://tailscale.com/install.sh | sh

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
