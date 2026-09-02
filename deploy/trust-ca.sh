#!/usr/bin/env bash
# Trust the gate pass server's certificate, on THIS machine.
#
#   ./deploy/trust-ca.sh                          # fetches it from the server
#   ./deploy/trust-ca.sh /path/to/fanzart-ca.pem  # or from a file you were given
#
# Run this once on each office machine. Until it has been run, the browser shows
# "Not secure" and staff have to click through a security warning, which is a
# habit worth not teaching.
#
# Linux only. On Windows and macOS, double-click the .pem — the instructions
# printed at the end of deploy/make-cert.sh cover both.
#
# The certificate this installs is PUBLIC. It lets this machine verify the gate
# pass server and confers nothing else: it cannot sign anything, cannot read
# anything, and does not give the server any access here.

set -uo pipefail

SERVER="${GP_SERVER:-fanzart-server.local}"
CA_NAME="Fanzart Gate Pass Local CA"
say()  { printf "\n\033[1m==> %s\033[0m\n" "$*"; }
note() { printf "    %s\n" "$*"; }
die()  { printf "\n\033[31mERROR: %s\033[0m\n" "$*" >&2; exit 1; }

say "Getting the certificate"
CA="${1:-}"
if [ -n "$CA" ]; then
    [ -f "$CA" ] || die "no such file: $CA"
    note "using $CA"
else
    CA="$(mktemp /tmp/fanzart-ca.XXXXXX.pem)"
    # Over plain HTTP on purpose: this is the file that MAKES https trustworthy,
    # so it cannot be fetched over a connection that needs it. That is safe for
    # a public certificate on the office LAN, and the fingerprint below is what
    # you check if you want to be sure of what you installed.
    curl -fsS --max-time 15 "http://$SERVER/fanzart-ca.pem" -o "$CA" \
        || die "could not fetch http://$SERVER/fanzart-ca.pem
       Set GP_SERVER=<address> if the server is somewhere else, or pass the
       file as an argument."
    note "fetched from http://$SERVER/fanzart-ca.pem"
fi

openssl x509 -in "$CA" -noout -subject >/dev/null 2>&1 \
    || die "that file is not a certificate"
grep -q "CA:TRUE" <(openssl x509 -in "$CA" -noout -text) \
    || die "that certificate is not a certificate authority — wrong file?"

note "subject:     $(openssl x509 -in "$CA" -noout -subject | sed 's/subject=//')"
note "expires:     $(openssl x509 -in "$CA" -noout -enddate | sed 's/notAfter=//')"
note "fingerprint: $(openssl x509 -in "$CA" -noout -fingerprint -sha256 | sed 's/.*=//')"

say "System store (curl, wget, python, Chromium on some systems)"
if [ "$(id -u)" -eq 0 ] || sudo -n true 2>/dev/null; then
    SUDO=""; [ "$(id -u)" -eq 0 ] || SUDO="sudo"
    $SUDO cp "$CA" /usr/local/share/ca-certificates/fanzart-gate-pass-ca.crt
    $SUDO update-ca-certificates >/dev/null 2>&1 \
        && note "installed" || note "update-ca-certificates failed"
else
    note "needs sudo — run:"
    note "  sudo cp $CA /usr/local/share/ca-certificates/fanzart-gate-pass-ca.crt"
    note "  sudo update-ca-certificates"
fi

say "Browser store (Chrome, Brave, Edge, Chromium)"
# The part the usual advice gets wrong. On Linux these browsers do NOT read
# /usr/local/share/ca-certificates — they keep their own NSS database in
# ~/.pki/nssdb. update-ca-certificates alone leaves them still warning, which
# looks like the certificate did not work.
if ! command -v certutil >/dev/null; then
    note "certutil is missing — install it, then run this again:"
    note "  sudo apt install libnss3-tools"
else
    mkdir -p "$HOME/.pki/nssdb"
    [ -f "$HOME/.pki/nssdb/cert9.db" ] || certutil -d "sql:$HOME/.pki/nssdb" -N --empty-password >/dev/null 2>&1
    certutil -d "sql:$HOME/.pki/nssdb" -D -n "$CA_NAME" >/dev/null 2>&1 || true
    if certutil -d "sql:$HOME/.pki/nssdb" -A -t "C,," -n "$CA_NAME" -i "$CA" 2>/dev/null; then
        note "installed for $(whoami) — restart the browser"
    else
        note "certutil would not add it; try the browser's own settings"
    fi
fi

say "Firefox"
# Firefox keeps a separate store again, one per profile, and has no supported
# command-line way in. Its own UI is the reliable route.
note "Settings -> Privacy & Security -> Certificates -> View Certificates"
note "  -> Authorities -> Import -> pick the file -> tick \"identify websites\""
note "file to import: $CA"

say "Checking"
if curl -fsS --max-time 10 -o /dev/null "https://$SERVER/login" 2>/dev/null; then
    note "https://$SERVER/login verifies — the padlock will be clean"
else
    note "curl still does not verify https://$SERVER/login"
    note "  If the system store step needed sudo, run those two lines and retry."
    note "  If the server's IP changed, its certificate must be reissued:"
    note "    sudo /opt/gate-pass/deploy/make-cert.sh && sudo /opt/gate-pass/deploy/enable-https.sh"
fi
echo
note "Browsers cache certificate decisions — close every window and reopen."
echo
