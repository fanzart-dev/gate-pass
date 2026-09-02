# Setting up an office machine

The gate pass server is at **`https://fanzart-server.local`**.

Each machine needs the server's certificate installed **once**. Until it is,
the browser says "Not secure" and staff have to click through a security
warning — a habit worth not teaching, and the reason this is worth ten minutes
per machine.

## Why a machine has to be told anything at all

No public certificate authority will vouch for `fanzart-server.local`. It is
not a real domain and nobody can prove they own it, so no authority the world
already trusts will sign a certificate for it.

The server therefore runs a tiny authority of its own. Installing
`fanzart-ca.pem` tells this machine "certificates signed by that authority are
genuine", and the warnings stop. It is a **public** certificate: it lets the
machine verify the server and nothing else — it cannot sign anything, read
anything, or give the server any access to the machine.

## Windows

1. Open `http://fanzart-server.local/fanzart-ca.pem` — it downloads.
2. Right-click it → **Install Certificate**
3. Choose **Local Machine** (not Current User, or only you get it)
4. **Place all certificates in the following store** → Browse →
   **Trusted Root Certification Authorities**
5. Close every browser window and reopen.

Or run `deploy/trust-ca-windows.ps1` as Administrator, which does all of that
and checks the result.

**Firefox keeps its own store** and ignores the one above:
Settings → Privacy & Security → Certificates → View Certificates →
Authorities → Import → tick "identify websites".

### If Kaspersky is installed, that is not enough

Kaspersky inspects HTTPS by intercepting it. It terminates the connection
itself, checks the site's certificate against **its own** list of authorities,
and re-encrypts to the browser with a certificate of its own. The Windows
Trusted Root store is not that list — so the browser is trusting Kaspersky, and
Kaspersky is refusing the server. This is what

> Visiting an untrustworthy website has been prevented

means here. Nothing is wrong with the server; Kaspersky has simply never been
told about our authority.

**Add the server to Kaspersky's trusted addresses.** Roughly:

> Settings → Network settings → Encrypted connection scanning →
> Trusted addresses / Manage exclusions → add `fanzart-server.local`

The exact wording moves between Kaspersky versions and between the home and
business products; look for "encrypted connection" together with "trusted" or
"exclusions". Turning encrypted-connection scanning off entirely also works,
but it stops Kaspersky inspecting *every* HTTPS site on that machine, so prefer
the exclusion.

**If neither is possible** — a locked corporate policy, say — use plain HTTP on
that machine:

    http://fanzart-server.local

The traffic never leaves the office LAN. That is a reasonable position; it is
where this server was until recently, and it is better than teaching people to
click past security warnings.

## Linux

```bash
curl -fsSO http://fanzart-server.local/fanzart-ca.pem
sudo apt install -y libnss3-tools     # browsers keep their own store
bash trust-ca.sh fanzart-ca.pem
```

`deploy/trust-ca.sh` installs into all three stores a Linux machine has — the
system one, the NSS database Chrome and Brave read, and each Firefox profile —
then verifies. **`sudo update-ca-certificates` alone is not enough**: Chrome
and Brave do not read the system store on Linux, so they carry on warning and
it looks as though the certificate did not work.

## macOS

1. Download `http://fanzart-server.local/fanzart-ca.pem`
2. Double-click → Keychain Access opens → add it to the **System** keychain
3. Find "Fanzart Gate Pass Local CA", open it, expand **Trust**, set
   **When using this certificate: Always Trust**
4. Close every browser window and reopen.

## Phones and tablets

Android: Settings → Security → Encryption & credentials → Install a
certificate → CA certificate. iOS: open the file in Safari to install the
profile, then Settings → General → About → Certificate Trust Settings and
switch it on. Both warn loudly while installing a CA — that warning is correct
and is why this is done by hand rather than pushed out silently.

## Checking it worked

Open `https://fanzart-server.local`. The padlock should be plain, with no
warning and no "Not secure".

Browsers cache certificate decisions hard: **close every window** and reopen,
not just the tab.

## When it stops working

**If the server's IP changes.** The certificate lists the addresses it is valid
for, and `192.168.1.45` is one of them. If DHCP moves the server, reissue it:

```bash
sudo /opt/gate-pass/deploy/make-cert.sh
sudo /opt/gate-pass/deploy/enable-https.sh
```

The authority does not change, so machines that already trust it keep working —
only the server certificate is replaced. Better still, give the server a fixed
address on the router and the problem does not arise.

**The certificate expires** after 825 days; the authority lasts ten years.
Reissuing is the same two commands, and again no machine needs redoing.

## Turning it off

If HTTPS is ever the reason the office cannot issue a gate pass:

```bash
sudo /opt/gate-pass/deploy/disable-https.sh
```

Plain HTTP comes straight back and nothing is lost. The certificates stay where
they are, so turning it on again is one command.
