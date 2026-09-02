#!/usr/bin/env bash
# Make the TLS certificate for in-house HTTPS.
#
#   sudo deploy/make-cert.sh [extra-name-or-ip ...]
#
# There is no public certificate authority that will vouch for a name like
# `fanzart-server.local` — it is not a real domain and nobody can prove they own
# it. So we run a tiny authority of our own: a CA certificate that is created
# once and installed on the office machines, and a server certificate signed by
# it. That is exactly what `mkcert` does; this does it with openssl, which is
# already on the machine.
#
# The distinction that matters:
#
#   * A bare self-signed certificate makes EVERY browser warn, every time, on
#     every machine, for ever. Staff learn to click through security warnings,
#     which is worse than plain HTTP because it teaches the wrong habit.
#   * A local CA warns until you install the CA once per machine, and then
#     never again. Same encryption either way; the difference is whether the
#     padlock can be trusted to mean anything.
#
# Install deploy/certs/fanzart-ca.pem on each office machine (see the notes
# printed at the end) and the warnings stop.

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CERT_DIR="$APP_DIR/deploy/certs"
DAYS_CA=3650      # the CA is installed by hand, so make it long-lived
DAYS_LEAF=825     # browsers refuse server certs valid for much longer than this

mkdir -p "$CERT_DIR"
# 755, not 700. The CA CERTIFICATE is meant to be handed out — every office
# machine needs a copy before the padlock means anything — and locking the
# directory made the one file people need impossible to fetch without root.
# The private keys inside are 600, which is what actually matters.
chmod 755 "$CERT_DIR"

HOSTNAME_SHORT="$(hostname)"
LAN_IP="$(hostname -I | awk '{print $1}')"
TS_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"

# Every name and address the office might type. A certificate is only valid for
# the names listed in it, so a missing one here is a warning later.
ALT=("DNS:${HOSTNAME_SHORT}.local" "DNS:${HOSTNAME_SHORT}" "DNS:localhost"
     "IP:127.0.0.1")
[ -n "$LAN_IP" ] && ALT+=("IP:$LAN_IP")
[ -n "$TS_IP" ]  && ALT+=("IP:$TS_IP")
for extra in "$@"; do
    case "$extra" in
        *[0-9].[0-9]*) ALT+=("IP:$extra") ;;
        *)             ALT+=("DNS:$extra") ;;
    esac
done
SAN="$(IFS=,; echo "${ALT[*]}")"

echo "Certificate will be valid for:"
printf '    %s\n' "${ALT[@]}"
echo

# --- the authority, created once and reused ---------------------------------
if [ -f "$CERT_DIR/fanzart-ca.pem" ] && [ -f "$CERT_DIR/fanzart-ca.key" ]; then
    echo "Using the existing local CA (installed machines keep working)."
else
    echo "Creating a local certificate authority..."
    openssl req -x509 -newkey rsa:4096 -sha256 -days "$DAYS_CA" -nodes \
        -keyout "$CERT_DIR/fanzart-ca.key" -out "$CERT_DIR/fanzart-ca.pem" \
        -subj "/O=Fanzart/CN=Fanzart Gate Pass Local CA" \
        -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
    echo "  new CA — it must be installed on every office machine (see below)"
fi

# --- the server certificate, reissued whenever the names change -------------
echo "Issuing the server certificate..."
openssl req -newkey rsa:2048 -sha256 -nodes \
    -keyout "$CERT_DIR/server.key" -out "$CERT_DIR/server.csr" \
    -subj "/O=Fanzart/CN=${HOSTNAME_SHORT}.local" 2>/dev/null

openssl x509 -req -in "$CERT_DIR/server.csr" -sha256 -days "$DAYS_LEAF" \
    -CA "$CERT_DIR/fanzart-ca.pem" -CAkey "$CERT_DIR/fanzart-ca.key" -CAcreateserial \
    -out "$CERT_DIR/server.pem" \
    -extfile <(printf 'subjectAltName=%s\nbasicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n' "$SAN") \
    2>/dev/null
rm -f "$CERT_DIR/server.csr"

# nginx reads these as root; nobody else needs the private key.
chmod 600 "$CERT_DIR/fanzart-ca.key" "$CERT_DIR/server.key"
chmod 644 "$CERT_DIR/fanzart-ca.pem" "$CERT_DIR/server.pem"

echo
echo "Written to $CERT_DIR:"
echo "    server.pem / server.key   used by nginx"
echo "    fanzart-ca.pem            install this on the office machines"
echo
openssl x509 -in "$CERT_DIR/server.pem" -noout -subject -dates \
    -ext subjectAltName | sed 's/^/    /'
cat <<'NOTES'

To stop the browser warning, install the CA once per machine:

  Windows   double-click fanzart-ca.pem -> Install Certificate ->
            Local Machine -> "Trusted Root Certification Authorities"
  macOS     double-click -> Keychain Access -> System -> set to "Always Trust"
  Linux     deploy/trust-ca.sh does all of it, including the part below that
            the usual advice misses
  Chrome/   On Linux these do NOT read the system store. They keep their own
  Brave     NSS database, so update-ca-certificates alone leaves them still
            warning and it looks like the certificate did not work:
              sudo apt install libnss3-tools
              certutil -d sql:$HOME/.pki/nssdb -A -t "C,," \
                       -n "Fanzart Gate Pass Local CA" -i fanzart-ca.pem
  Firefox   keeps its own store again: Settings -> Certificates -> View
            Certificates -> Authorities -> Import -> tick "identify websites"
  Android   Settings -> Security -> Encryption -> Install from storage -> CA

Until that is done the padlock will still be crossed out. The traffic is
encrypted either way; the warning is the browser saying it cannot verify WHO
it is talking to, which is only answerable by trusting the CA above.
NOTES
