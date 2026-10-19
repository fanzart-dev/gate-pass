#!/usr/bin/env bash
# Make the public links work in the office even when the office internet line
# cannot reach Tailscale's relay.
#
#   sudo deploy/enable-office-link.sh            # set it up (safe to re-run)
#   deploy/enable-office-link.sh --status        # is it working? what is broken?
#   sudo deploy/enable-office-link.sh --renew    # certificates + new apps (the timer does this daily)
#   sudo deploy/enable-office-link.sh --off      # take the office path away again
#
# WHAT WENT WRONG
# ---------------------------------------------------------------------------
# https://<name>.ts.net is published by Tailscale Funnel. A browser that is not
# on Tailscale reaches it through Tailscale's relay on the public internet, so
# a PC in the office goes office -> ISP -> relay -> ISP -> this server. On
# 17-10-2026 the office ISP stopped connecting to that relay at all (it had
# been dropping it for a few minutes a day all week), and every office PC
# without Tailscale got ERR_TIMED_OUT — while the same link worked from the
# rest of the world, and nothing on this server was wrong.
#
# WHAT THIS DOES
# Gives every public name on this server a second way in that never leaves
# the building. "Every" means this server's own Tailscale name (Gate Pass 443,
# FanIQ 8443, MailStream 10000) AND each Tailscale node running in a Docker
# container here (the FZ service app, fz-service.<tailnet>.ts.net), found by
# looking, so an app added later is picked up without editing this file:
#
#   1. Fetches each name's genuine Let's Encrypt certificate from its own
#      tailscaled (`tailscale cert`) — the same one the relay shows.
#   2. Has nginx answer each name, on each port it publishes through Funnel,
#      on the LAN address with that certificate, handing over to exactly what
#      Funnel hands over to. Funnel ports only: a tailnet-only port stays so.
#   3. Runs a small DNS server on the LAN address (dnsmasq, already on the
#      machine) that answers those names with the LAN address and passes every
#      other lookup to the router, exactly as before.
#   4. Daily (systemd timer): renews the certificates and picks up any new
#      Funnel port or app, reloading only if something changed.
#
# Then ONE change on the router, and no PC is touched: DHCP -> Primary DNS =
# this server, Secondary DNS = the router. If this server is ever off, PCs
# fall back to the secondary and everything but the links keeps working.
# Everyone outside the office is unaffected.

set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TLS_DIR=/etc/gate-pass/tailnet-tls
SITE=/etc/nginx/sites-available/zz-gate-pass-office-link
DNS_CONF=/etc/gate-pass/office-dns.conf
MODE="${1:-}"

say()  { printf "\n\033[1m==> %s\033[0m\n" "$*"; }
note() { printf "    %s\n" "$*"; }
die()  { printf "\n\033[31mERROR: %s\033[0m\n" "$*" >&2; exit 1; }

case "$MODE" in ""|--renew|--off|--status) ;; *) die "unknown option: $MODE" ;; esac
[ "$MODE" = "--status" ] || [ "$(id -u)" -eq 0 ] || die "run with sudo"

valid_name() { [[ "$1" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ ]]; }

# The office address: the one the default route leaves by, which is the
# address the router knows this server as.
GATEWAY="$(ip -4 route show default 2>/dev/null | awk '{print $3; exit}')"
LAN_IP="$(ip -4 route get "${GATEWAY:-192.168.1.1}" 2>/dev/null | grep -oP 'src \K[0-9.]+' | head -1)"

# ------------------------------------------------------------------ off ----
if [ "$MODE" = "--off" ]; then
    say "Removing the office path"
    # Put the router's DNS back BEFORE running this, or PCs are sent to a
    # server that no longer answers the names.
    systemctl disable --now gate-pass-tailnet-cert.timer gate-pass-office-dns.service >/dev/null 2>&1
    rm -f /etc/systemd/system/gate-pass-tailnet-cert.service \
          /etc/systemd/system/gate-pass-tailnet-cert.timer \
          /etc/systemd/system/gate-pass-office-dns.service
    systemctl daemon-reload
    # name is what enable-https.sh keys on; the certificates are left so
    # turning it back on is instant.
    rm -f "$TLS_DIR/name" "$TLS_DIR/nodes"
    rm -f /etc/nginx/sites-enabled/zz-gate-pass-office-link "$SITE"
    "$APP_DIR/deploy/enable-https.sh" || die "enable-https.sh failed"
    note "off. Set the router's DHCP DNS back to automatic, and undo any"
    note "fix-name-windows.ps1 -Name <name> on PCs (-Undo -Name <name>)."
    exit 0
fi

# ------------------------------------------------------------ discovery ----
# Every Tailscale node on this machine, one line each:
#   NAME  CONTAINER  SOCKET  COMPOSE_PROJECT
# CONTAINER is "-" for this server's own tailscaled.
self_name() {
    tailscale status --json 2>/dev/null \
      | python3 -c 'import json,sys; print((json.load(sys.stdin).get("Self") or {}).get("DNSName","").rstrip("."))' 2>/dev/null
}
ts() {          # ts CONTAINER SOCKET args... — run tailscale on that node
    local c="$1" s="$2"; shift 2
    if [ "$c" = "-" ]; then tailscale "$@"
    else docker exec "$c" tailscale --socket="$s" "$@"; fi
}
discover() {
    local name c image sock project
    name="$(self_name)"
    valid_name "$name" && printf '%s - - -\n' "$name"
    command -v docker >/dev/null 2>&1 || return 0
    docker ps --format '{{.Names}} {{.Image}}' 2>/dev/null | while read -r c image; do
        case "$image" in tailscale/tailscale*|ghcr.io/tailscale/tailscale*) ;; *) continue ;; esac
        sock="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$c" 2>/dev/null \
                | sed -n 's/^TS_SOCKET=//p' | head -1)"
        sock="${sock:-/tmp/tailscaled.sock}"
        project="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$c" 2>/dev/null)"
        name="$(ts "$c" "$sock" status --json 2>/dev/null \
                | python3 -c 'import json,sys; print((json.load(sys.stdin).get("Self") or {}).get("DNSName","").rstrip("."))' 2>/dev/null)"
        valid_name "$name" && printf '%s %s %s %s\n' "$name" "$c" "$sock" "${project:--}"
    done
}

# Funnel ports of one node, "PORT TARGET" per line. Read from tailscaled,
# Funnel ports only: a port that is tailnet-only stays tailnet-only, rather
# than being opened to the LAN by this script.
funnel_ports() {
    local name="$1" c="$2" sock="$3"
    ts "$c" "$sock" serve status --json 2>/dev/null | NAME="$name" python3 -c '
import json, os, sys, re
name = os.environ["NAME"]
cfg = json.load(sys.stdin) or {}
funnel = cfg.get("AllowFunnel") or {}
for hostport, web in sorted((cfg.get("Web") or {}).items()):
    host, _, port = hostport.rpartition(":")
    if host != name or not funnel.get(hostport) or not port.isdigit():
        continue
    target = ((web.get("Handlers") or {}).get("/") or {}).get("Proxy", "")
    if re.fullmatch(r"http://[A-Za-z0-9._-]+:\d+/?", target):
        print(port, target.rstrip("/"))
'
}

# Where nginx on the host can reach what a node's Funnel proxies to.
#   own node:   http://127.0.0.1:N is already a host address.
#   container:  http://<service>:N is a compose service — reach it through the
#               port that service publishes on the host, which survives the
#               container being rebuilt (its internal address does not).
host_target() {
    local c="$1" project="$2" target="$3" host port svc hp
    host="${target#http://}"; port="${host##*:}"; host="${host%:*}"
    if [ "$c" = "-" ]; then
        [ "$host" = "127.0.0.1" ] && echo "http://127.0.0.1:$port"
        return 0
    fi
    [ "$project" != "-" ] || return 0
    svc="$(docker ps --filter "label=com.docker.compose.project=$project" \
                     --filter "label=com.docker.compose.service=$host" \
                     --format '{{.Names}}' 2>/dev/null | head -1)"
    [ -n "$svc" ] || return 0
    hp="$(docker port "$svc" "$port/tcp" 2>/dev/null | grep -oP '^(0\.0\.0\.0|127\.0\.0\.1):\K[0-9]+' | head -1)"
    [ -n "$hp" ] && echo "http://127.0.0.1:$hp"
}

# ---------------------------------------------------------- certificates ----
# fetch NAME CONTAINER SOCKET -> prints "same" or "new". Written beside the
# live files and moved into place, so nginx never reads a half-written key,
# and a failed fetch leaves the working pair alone.
fetch() {
    local name="$1" c="$2" sock="$3" err
    mkdir -p "$TLS_DIR" && chmod 755 "$TLS_DIR"
    local crt="$TLS_DIR/.new.crt" key="$TLS_DIR/.new.key"
    rm -f "$crt" "$key"
    if [ "$c" = "-" ]; then
        if ! err="$(tailscale cert --cert-file "$crt" --key-file "$key" "$name" 2>&1)"; then
            rm -f "$crt" "$key"; printf '%s\n' "$err" >&2; return 1
        fi
    else
        # Inside the container, then read out; the copies there are removed.
        local tmp="/tmp/gate-pass-office-link.$$"
        if ! err="$(docker exec "$c" tailscale --socket="$sock" cert \
                        --cert-file "$tmp.crt" --key-file "$tmp.key" "$name" 2>&1)"; then
            printf '%s\n' "$err" >&2; return 1
        fi
        (umask 077; docker exec "$c" cat "$tmp.key" > "$key") \
          && docker exec "$c" cat "$tmp.crt" > "$crt"
        local ok=$?
        docker exec "$c" rm -f "$tmp.crt" "$tmp.key" >/dev/null 2>&1
        [ "$ok" -eq 0 ] && [ -s "$crt" ] && [ -s "$key" ] \
            || { rm -f "$crt" "$key"; echo "could not read the certificate out of $c" >&2; return 1; }
    fi
    chmod 644 "$crt"; chmod 600 "$key"
    if cmp -s "$crt" "$TLS_DIR/$name.crt" && cmp -s "$key" "$TLS_DIR/$name.key"; then
        rm -f "$crt" "$key"
        echo same
    else
        mv -f "$key" "$TLS_DIR/$name.key"
        mv -f "$crt" "$TLS_DIR/$name.crt"
        echo new
    fi
}

days_left() {
    local end
    end="$(openssl x509 -noout -enddate -in "$1" 2>/dev/null | cut -d= -f2)" || return 1
    [ -n "$end" ] && echo $(( ($(date -d "$end" +%s) - $(date +%s)) / 86400 ))
}

# --------------------------------------------------------------- render ----
# Builds the extra-port nginx site and the DNS settings from what discovery
# found, into temporary files: RENDERED_SITE, RENDERED_DNS, and NAMES_PORTS
# (one "NAME PORT" per line, for the checks).
render() {
    local name c sock project port target ht
    RENDERED_SITE="$(mktemp)"; RENDERED_DNS="$(mktemp)"; NAMES_PORTS=""
    {
        echo "# Written by $APP_DIR/deploy/enable-office-link.sh — re-run it, do not edit."
        echo "map \$http_upgrade \$gp_office_upgrade { default upgrade; \"\" \"\"; }"
    } > "$RENDERED_SITE"
    {
        echo "# Written by $APP_DIR/deploy/enable-office-link.sh — re-run it, do not edit."
        echo "# The office PCs' DNS once the router hands this server out as DNS."
        echo "listen-address=$LAN_IP"
        echo "bind-dynamic"
        echo "port=53"
        echo "# Everything else goes to the router, which is what the PCs asked before."
        echo "no-resolv"
        echo "no-hosts"
        echo "server=$GATEWAY"
        echo "cache-size=2000"
        echo "user=nobody"
        echo "group=nogroup"
        echo "pid-file="
        echo "# Each public name, and anything under it, is this server. local= makes"
        echo "# this server the authority for it, so the relay's IPv6 addresses (and"
        echo "# HTTPS records) are not passed through as a way back to the relay."
    } > "$RENDERED_DNS"
    : > "$TLS_DIR/nodes.new"
    while read -r name c sock project; do
        [ -n "$name" ] || continue
        if [ ! -s "$TLS_DIR/$name.crt" ] || [ ! -s "$TLS_DIR/$name.key" ]; then
            note "skipping $name — no certificate for it yet"
            continue
        fi
        local served=""
        while read -r port target; do
            [ -n "$port" ] || continue
            # The gate pass site already answers this server's own name on 443.
            [ "$c" = "-" ] && [ "$port" = "443" ] && { served="$served 443"; continue; }
            ht="$(host_target "$c" "$project" "$target")"
            if [ -z "$ht" ]; then
                note "skipping $name:$port — cannot reach $target from the host"
                continue
            fi
            if [ "$port" != "443" ] && ss -tlnpH "( sport = :$port )" 2>/dev/null \
                   | grep -E "($LAN_IP|0\.0\.0\.0|\*):$port " | grep -vq 'users:(("nginx"'; then
                note "skipping $name:$port — something other than nginx holds $LAN_IP:$port"
                continue
            fi
            sed -e "s|__LAN_IP__|$LAN_IP|g" -e "s|__PORT__|$port|g" -e "s|__TARGET__|$ht|g" \
                -e "s|__TAILNET_NAME__|$name|g" -e "s|__TAILNET_TLS_DIR__|$TLS_DIR|g" \
                "$APP_DIR/deploy/nginx-gate-pass-office-ports.conf" >> "$RENDERED_SITE"
            served="$served $port"
        done <<< "$(funnel_ports "$name" "$c" "$sock")"
        [ -n "$served" ] || { note "skipping $name — nothing of it is published through Funnel"; continue; }
        printf 'local=/%s/\naddress=/%s/%s\n' "$name" "$name" "$LAN_IP" >> "$RENDERED_DNS"
        for port in $served; do NAMES_PORTS="$NAMES_PORTS$name $port"$'\n'; done
        [ "$c" = "-" ] || printf '%s %s %s %s\n' "$name" "$c" "$sock" "$project" >> "$TLS_DIR/nodes.new"
        note "$name, port(s)$served"
    done <<< "$NODES"
}

# Installs RENDERED_SITE / RENDERED_DNS if they differ from what is live.
# Prints what it changed. nginx is tested before reload, and put back if not.
apply() {
    local changed=""
    if ! cmp -s "$RENDERED_SITE" "$SITE"; then
        cp -f "$SITE" "$SITE.bak" 2>/dev/null || rm -f "$SITE.bak"
        cp -f "$RENDERED_SITE" "$SITE" && chmod 644 "$SITE"
        ln -sf "$SITE" /etc/nginx/sites-enabled/zz-gate-pass-office-link
        if ! nginx -t 2>/tmp/office-link-nginx.$$; then
            cat /tmp/office-link-nginx.$$ >&2; rm -f /tmp/office-link-nginx.$$
            if [ -f "$SITE.bak" ]; then cp -f "$SITE.bak" "$SITE"
            else rm -f "$SITE" /etc/nginx/sites-enabled/zz-gate-pass-office-link; fi
            return 1
        fi
        rm -f /tmp/office-link-nginx.$$
        changed="$changed nginx"
    fi
    ln -sf "$SITE" /etc/nginx/sites-enabled/zz-gate-pass-office-link
    if ! cmp -s "$RENDERED_DNS" "$DNS_CONF"; then
        cp -f "$RENDERED_DNS" "$DNS_CONF" && chmod 644 "$DNS_CONF"
        changed="$changed dns"
    fi
    mv -f "$TLS_DIR/nodes.new" "$TLS_DIR/nodes"
    rm -f "$RENDERED_SITE" "$RENDERED_DNS"
    echo "$changed"
}

check_paths() {     # every NAME PORT answers on the LAN with a valid certificate
    local name port code bad=0
    while read -r name port; do
        [ -n "$name" ] || continue
        code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
                    --resolve "$name:$port:$LAN_IP" "https://$name:$port/")
        case "$code" in
            2*|3*|401|403|404) note "OK    https://$name:$port -> $code (genuine certificate)" ;;
            *) note "FAIL  https://$name:$port -> '${code:-no answer}'"; bad=1 ;;
        esac
    done <<< "$NAMES_PORTS"
    return $bad
}

check_dns() {
    local name got bad=0
    for name in $(awk '{print $1}' <<< "$NAMES_PORTS" | sort -u); do
        got="$(dig +short +time=3 +tries=1 @"$LAN_IP" "$name" A 2>/dev/null | head -1)"
        if [ "$got" = "$LAN_IP" ]; then note "OK    office DNS: $name -> $got"
        else note "FAIL  office DNS: $name -> '${got:-no answer}'"; bad=1; fi
    done
    got="$(dig +short +time=3 +tries=1 @"$LAN_IP" github.com A 2>/dev/null | head -1)"
    if [ -n "$got" ]; then note "OK    office DNS passes other names on (github.com -> $got)"
    else note "FAIL  office DNS does not answer other names — PCs fall back to the router"; bad=1; fi
    return $bad
}

[ -n "$GATEWAY" ] && [ -n "$LAN_IP" ] || die "could not work out the LAN address and router"

# --------------------------------------------------------------- status ----
if [ "$MODE" = "--status" ]; then
    say "Office path — $LAN_IP, router $GATEWAY"
    if [ ! -f "$TLS_DIR/name" ]; then
        note "NOT SET UP. Run:  sudo $APP_DIR/deploy/enable-office-link.sh"
        exit 1
    fi
    systemctl is-active --quiet gate-pass-office-dns.service \
        && note "OK    office DNS service running" \
        || note "FAIL  office DNS service is not running:  sudo systemctl restart gate-pass-office-dns"
    systemctl is-active --quiet gate-pass-tailnet-cert.timer \
        && note "OK    daily renewal timer active" \
        || note "FAIL  renewal timer is off — re-run: sudo $APP_DIR/deploy/enable-office-link.sh"
    NAMES_PORTS=""
    for name in $(cat "$TLS_DIR/name") $(awk '{print $1}' "$TLS_DIR/nodes" 2>/dev/null); do
        d="$(days_left "$TLS_DIR/$name.crt")"
        if [ -n "$d" ] && [ "$d" -gt 14 ]; then note "OK    certificate for $name: $d days left"
        else note "FAIL  certificate for $name: ${d:-unreadable} days left — sudo $0 --renew"; fi
        NAMES_PORTS="$NAMES_PORTS$name 443"$'\n'
    done
    # The extra ports, as nginx serves them.
    NAMES_PORTS="$NAMES_PORTS$(awk '/^[ \t]*listen /{split($2,a,":"); p=a[2]+0} /^[ \t]*server_name /{n=$2; sub(";","",n); if (p && p != 443) print n, p}' "$SITE" 2>/dev/null)"$'\n'
    say "Inside the office (what PCs get once the router points DNS here)"
    check_dns; d1=$?
    check_paths; d2=$?
    say "Through Tailscale's relay, from this office line"
    for name in $(awk '{print $1}' <<< "$NAMES_PORTS" | sort -u); do
        for ip in $(dig +short @1.1.1.1 "$name" A 2>/dev/null | grep -E '^[0-9.]+$'); do
            r="$(curl -s -o /dev/null --max-time 8 -w '%{http_code} %{time_connect} %{time_total}' \
                      --resolve "$name:443:$ip" "https://$name/" 2>/dev/null)"
            read -r code conn total <<< "$r"
            if [ "${code:-000}" != "000" ]; then note "up    $name via $ip -> $code in ${total}s"
            elif awk "BEGIN{exit !(${conn:-0} == 0)}"; then note "DOWN  $name via $ip — this line cannot connect to the relay (ISP route)"
            else note "DOWN  $name via $ip — connected, no answer within 8s"; fi
        done
    done
    cat <<'HOW'

    How to read this:
      * Relay DOWN but everything under "Inside the office" OK
          -> the ISP route again. Office PCs whose DNS is this server are
             unaffected. If office PCs still fail: on one of them run
                 nslookup <name>
             It must say 192.168.1.45. If it shows 103.84.x.x, that PC is
             not using this server for DNS — check the router's DHCP
             Primary DNS, then restart that PC or reconnect its network.
      * Anything FAIL under "Inside the office"
          -> sudo /opt/gate-pass/deploy/enable-office-link.sh   (repairs it)
HOW
    [ "$d1" -eq 0 ] && [ "$d2" -eq 0 ]
    exit $?
fi

# ---------------------------------------------------------------- renew ----
NODES="$(discover)"
NAME="$(head -1 <<< "$NODES" | awk '$2 == "-" {print $1}')"
[ -n "$NAME" ] || die "tailscaled is not answering — sudo systemctl status tailscaled"

if [ "$MODE" = "--renew" ]; then
    [ -f "$TLS_DIR/name" ] || { note "office path is off; nothing to renew"; exit 0; }
    renewed=""
    while read -r name c sock project; do
        [ -n "$name" ] || continue
        result="$(fetch "$name" "$c" "$sock")" \
            || { note "WARNING: could not renew $name — its current certificate stays in use"; continue; }
        [ "$result" = "new" ] && renewed="$renewed $name"
        note "$name: $(days_left "$TLS_DIR/$name.crt") days left"
    done <<< "$NODES"
    # New apps / ports are picked up here too, so nobody has to remember to.
    render >/dev/null
    changed="$(apply)" || die "nginx rejected the updated office-link site — the previous one is still live"
    if [ -n "$renewed" ] || [[ "$changed" == *nginx* ]]; then
        nginx -t >/dev/null 2>&1 || die "nginx rejected the config after renewal"
        systemctl reload nginx || die "nginx would not reload"
        note "nginx reloaded (renewed:${renewed:- none}; site ${changed:-unchanged})"
    fi
    if [[ "$changed" == *dns* ]]; then
        systemctl restart gate-pass-office-dns.service && note "office DNS updated"
    fi
    exit 0
fi

# ---------------------------------------------------------------- setup ----
grep -q '443 ssl' /etc/nginx/sites-available/gate-pass 2>/dev/null \
    || die "HTTPS is not on for the LAN yet — run sudo deploy/enable-https.sh first"
grep -q "listen $LAN_IP:443 ssl" /etc/nginx/sites-available/gate-pass \
    || die "the gate pass site does not listen on $LAN_IP:443 — run sudo deploy/enable-https.sh"
note "LAN address $LAN_IP, router $GATEWAY"

say "Certificates"
while read -r name c sock project; do
    [ -n "$name" ] || continue
    if fetch "$name" "$c" "$sock" >/dev/null; then
        note "$name: valid for $(days_left "$TLS_DIR/$name.crt") days$([ "$c" = "-" ] || echo " (from container $c)")"
    elif [ "$c" = "-" ]; then
        die "tailscale cert failed for $name. HTTPS certificates must be enabled for
       the tailnet (admin console -> DNS -> HTTPS Certificates); Funnel needs them,
       so if the public link has ever worked they already are."
    else
        note "WARNING: no certificate for $name (container $c) — it is left out"
    fi
done <<< "$NODES"
printf '%s\n' "$NAME" > "$TLS_DIR/name"

say "Daily renewal"
for unit in gate-pass-tailnet-cert.service gate-pass-tailnet-cert.timer; do
    [ -f "$APP_DIR/deploy/$unit" ] || die "missing $APP_DIR/deploy/$unit"
    sed "s|__APP_DIR__|$APP_DIR|g" "$APP_DIR/deploy/$unit" \
        > "/etc/systemd/system/$unit" || die "could not write $unit"
done
systemctl daemon-reload
systemctl enable --now gate-pass-tailnet-cert.timer >/dev/null 2>&1 \
    || die "could not start the renewal timer"
note "systemctl list-timers gate-pass-tailnet-cert"

say "nginx: $NAME on 443"
"$APP_DIR/deploy/enable-https.sh" || die "enable-https.sh failed — see above"

say "nginx: every other public name and port"
# Port 53 on the LAN address must be free, or ours. systemd-resolved holds it
# on 127.0.0.53 only, which is no conflict.
holder="$(ss -ulnpH "( sport = :53 )" 2>/dev/null | grep -E "($LAN_IP|0\.0\.0\.0|\*):53 " | grep -v 'dnsmasq')"
[ -z "$holder" ] || die "something else already answers DNS on $LAN_IP:53:
       $holder"
render
changed="$(apply)" || die "nginx rejected the office-link site — put back as it was"
systemctl reload nginx || die "nginx would not reload"
sleep 1
check_paths || note "(a FAIL above means that one app is not reachable by name in the office)"

say "Office DNS"
sed "s|__APP_DIR__|$APP_DIR|g" "$APP_DIR/deploy/gate-pass-office-dns.service" \
    > /etc/systemd/system/gate-pass-office-dns.service || die "could not write the DNS unit"
systemctl daemon-reload
systemctl enable gate-pass-office-dns.service >/dev/null 2>&1
systemctl restart gate-pass-office-dns.service || die "the office DNS would not start:
       journalctl -u gate-pass-office-dns -n 20 --no-pager"
sleep 1
check_dns || die "the office DNS is not answering correctly — journalctl -u gate-pass-office-dns -n 20"

cat <<DONE

    ------------------------------------------------------------------
    The server now answers every public link in the office, on
    $LAN_IP, and answers office DNS there:
$(awk '{print $1}' <<< "$NAMES_PORTS" | sort -u | sed 's/^/        https:\/\//')

    LAST STEP — once, on the router ($GATEWAY), nothing on the PCs:

      DHCP Server settings:
          Primary DNS    $LAN_IP
          Secondary DNS  $GATEWAY
      Save.

    PCs pick it up as their address lease renews; a restart, or
    unplugging and reconnecting the network, does it at once.

    Any time:  $APP_DIR/deploy/enable-office-link.sh --status
    ------------------------------------------------------------------

DONE
