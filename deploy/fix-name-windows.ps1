# Make fanzart-server.local work on a Windows office machine.
#
#   Right-click -> Run with PowerShell        (as Administrator)
#
# or from an Administrator PowerShell:
#
#   Set-ExecutionPolicy -Scope Process Bypass -Force
#   .\fix-name-windows.ps1
#
# To undo it later:   .\fix-name-windows.ps1 -Undo
#
#
# WHY THIS IS NEEDED AT ALL
# ---------------------------------------------------------------------------
# `fanzart-server.local` is not an ordinary name. The `.local` ending means
# mDNS: instead of asking a DNS server, the machine shouts the question at the
# whole network and waits for the server to shout back. Macs and Linux machines
# do this natively and it just works — which is why some machines in the office
# have never had a problem.
#
# Windows supports mDNS patchily, and three ordinary things switch it off:
#
#   * the network being marked Public rather than Private, which blocks the
#     inbound multicast the answer arrives on
#   * security software (Kaspersky is on these machines) filtering UDP 5353
#   * Wi-Fi client isolation, band steering, or a guest SSID on the router
#
# When it is off, the browser never resolves the name and says
# DNS_PROBE_FINISHED_NXDOMAIN. Nothing is wrong with the server: it is
# answering correctly, and this machine cannot hear it.
#
# So rather than trying to repair mDNS — which depends on the router, the
# Wi-Fi profile and the antivirus, none of which stay fixed — this writes the
# answer down locally, in the hosts file. The machine then already knows the
# address and never has to ask. That works regardless of Kaspersky, the Wi-Fi
# band, or whether the network is Public or Private.
#
# The trade is that the address is now written down: if the server ever moves
# to a different IP, this line becomes wrong and has to be updated. The server
# has a fixed address set on it for exactly this reason. This script verifies
# the address really is the gate pass server before writing anything, so a
# wrong -ServerIp cannot quietly point the whole office at nothing.
#
# NOT TESTED ON WINDOWS. It was written on Linux, where PowerShell is not
# available to run it, so treat the first run as the test. Every step prints
# what it did, and every step that can fail prints the manual equivalent.

param(
    [string]$ServerIp = "192.168.1.45",
    [string]$Name     = "fanzart-server.local",
    [switch]$Undo
)

$ErrorActionPreference = "Stop"
function Say  ($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Note ($m) { Write-Host "    $m" }
function Warn ($m) { Write-Host "    $m" -ForegroundColor Yellow }
function Good ($m) { Write-Host "    $m" -ForegroundColor Green }
function Fail ($m) { Write-Host "`nERROR: $m" -ForegroundColor Red; exit 1 }

# Not hardcoded to C:\, because Windows is not always on C:.
$hostsPath = Join-Path $env:SystemRoot "System32\drivers\etc\hosts"

$admin = ([Security.Principal.WindowsPrincipal] `
          [Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Fail "run this as Administrator (right-click PowerShell -> Run as administrator)"
}

# ---------------------------------------------------------------- undo ------
if ($Undo) {
    Say "Removing the entry for $Name"
    if (-not (Test-Path $hostsPath)) { Fail "no hosts file at $hostsPath" }
    $lines = Get-Content $hostsPath
    $kept  = $lines | Where-Object { $_ -notmatch "\s$([regex]::Escape($Name))\s*$" `
                                     -and $_ -notmatch "^\s*#\s*Fanzart Gate Pass" }
    if ($kept.Count -eq $lines.Count) {
        Note "there was no entry to remove"
    } else {
        # ASCII, not UTF8: PowerShell 5.1 writes a byte-order mark with UTF8,
        # and a BOM on the first line of hosts stops Windows parsing that line.
        Set-Content -Path $hostsPath -Value $kept -Encoding ASCII
        Good "removed"
    }
    ipconfig /flushdns | Out-Null
    Note "DNS cache flushed. $Name now depends on mDNS again."
    exit 0
}

# ------------------------------------------------- 1. is the server there ---
Say "Checking the server is reachable at $ServerIp"

# Ping first, because it separates "the network is wrong" from "the name is
# wrong" — and those have completely different answers. A machine that cannot
# ping the server is not on the office network, and no hosts entry will help.
if (Test-Connection -ComputerName $ServerIp -Count 2 -Quiet -ErrorAction SilentlyContinue) {
    Good "$ServerIp answers"
} else {
    Warn "$ServerIp does not answer a ping"
    Warn "Some networks block ping, so this is not conclusive — carrying on."
}

# The real test, and the one that matters: is this actually OUR server? A bare
# ping only proves something is at that address. Writing a hosts entry for the
# wrong machine would be worse than the problem being fixed, because the name
# would then resolve confidently to somewhere wrong and mDNS could never
# correct it. This file is served over plain HTTP, by IP, precisely so that a
# machine which cannot resolve or verify anything yet can still fetch it.
Say "Checking it is the gate pass server"
$probe = "http://$ServerIp/fanzart-ca.pem"
try {
    $body = (Invoke-WebRequest -Uri $probe -UseBasicParsing -TimeoutSec 10).Content
} catch {
    Fail @"
could not fetch $probe

        That address is not serving the gate pass. Either the server has moved
        to a different IP, or this machine is not on the office network.

        Check with whoever runs the server, then re-run with the right address:
            .\fix-name-windows.ps1 -ServerIp 192.168.1.NN
"@
}
if ($body -notmatch "BEGIN CERTIFICATE") {
    Fail "$probe answered, but not with the gate pass certificate — wrong machine at $ServerIp"
}
Good "confirmed: $ServerIp is the gate pass server"

# --------------------------------------------------- 2. why it fails now ----
Say "Checking how $Name resolves today"
$before = $null
try { $before = [System.Net.Dns]::GetHostAddresses($Name) } catch { }
if ($before) {
    Note "it already resolves to: $(($before | ForEach-Object { $_.IPAddressToString }) -join ', ')"
    if ($before.IPAddressToString -contains $ServerIp) {
        Note "which is correct — this machine's problem is not name resolution."
        Note "If the browser still fails, it is the certificate, not the name:"
        Note "  run trust-ca-windows.ps1, and see OFFICE-MACHINES.md for Kaspersky."
    } else {
        Warn "which is NOT the server — a stale entry is pointing somewhere wrong."
    }
} else {
    Note "it does not resolve at all — this is the NXDOMAIN the browser reports."
    Note "mDNS is not reaching this machine. Writing the answer down instead."
}

# Worth knowing WHY, not just working around it. One of these causes has a
# better fix than a hosts entry, and it is worth five seconds to find out:
# a network marked Public blocks the inbound multicast the mDNS answer arrives
# on, and switching it to Private fixes file sharing and network printers at
# the same time. The others are things a hosts entry genuinely is the answer
# to. Every check is wrapped, because this is diagnosis — it must never be the
# reason the repair does not run.
Say "Why mDNS is not working here"
try {
    $profiles = Get-NetConnectionProfile -ErrorAction Stop
    foreach ($p in $profiles) {
        if ($p.NetworkCategory -eq "Public") {
            Warn "'$($p.Name)' is set to Public, which blocks mDNS."
            Warn "  This is probably the cause, and it has a better fix than this script:"
            Warn "  Settings -> Network & Internet -> (the connection) -> set to Private."
            Warn "  Doing that also restores file sharing and network printers."
        } else {
            Note "'$($p.Name)' is $($p.NetworkCategory) — not the problem"
        }
    }
} catch { Note "(could not read the network profile)" }

try {
    $mdnsRules = Get-NetFirewallRule -ErrorAction Stop |
                 Where-Object { $_.DisplayName -match "mDNS" -and $_.Direction -eq "Inbound" }
    $on = @($mdnsRules | Where-Object { $_.Enabled -eq "True" }).Count
    if ($mdnsRules -and $on -eq 0) {
        Warn "every inbound mDNS firewall rule is disabled — the answer is blocked"
    } elseif ($mdnsRules) {
        Note "$on inbound mDNS firewall rule(s) enabled — the firewall is not blocking it"
    }
} catch { Note "(could not read the firewall rules)" }

if (Get-Service -Name "AVP*","klnagent*" -ErrorAction SilentlyContinue) {
    Warn "Kaspersky is running. It filters UDP 5353, which is how mDNS answers"
    Warn "  arrive, and it is a common reason .local stops working on Windows."
    Warn "  The hosts entry below sidesteps it entirely."
}

# ------------------------------------------------------- 3. write it down ---
Say "Updating $hostsPath"
Copy-Item $hostsPath "$hostsPath.bak" -Force
Note "backed up to $hostsPath.bak"

$marker = "# Fanzart Gate Pass - added by fix-name-windows.ps1"
$lines  = @(Get-Content $hostsPath -ErrorAction SilentlyContinue)

# Matched as the whole last field, not as a substring: a plain -match on the
# name would also strike out a line for "old-fanzart-server.local" and leave
# the machine worse off than it started.
$escaped = [regex]::Escape($Name)
$kept = $lines | Where-Object {
    $_ -notmatch "\s$escaped\s*$" -and $_ -notmatch "^\s*#\s*Fanzart Gate Pass"
}
$removed = $lines.Count - $kept.Count
if ($removed -gt 0) { Note "replaced $removed existing line(s) for $Name" }

$kept += $marker
$kept += "$ServerIp`t$Name"
Set-Content -Path $hostsPath -Value $kept -Encoding ASCII
Good "wrote: $ServerIp  $Name"

# Windows caches name lookups, including the failures. Without this the machine
# keeps serving itself the NXDOMAIN it learned earlier and the fix looks like
# it did nothing.
ipconfig /flushdns | Out-Null
Note "DNS cache flushed"

# ------------------------------------------------------------ 4. verify -----
Say "Verifying"
$after = $null
try { $after = [System.Net.Dns]::GetHostAddresses($Name) } catch { }
if (-not $after) {
    Fail "$Name still does not resolve. The hosts line was written, so something
        is overriding it — check $hostsPath is not read-only and that no policy
        or security product is managing it."
}
$got = ($after | ForEach-Object { $_.IPAddressToString }) -join ', '
if ($after.IPAddressToString -contains $ServerIp) {
    Good "$Name -> $got"
} else {
    Fail "$Name resolves to $got, not $ServerIp"
}

# Resolving is not the same as reaching. Tested against the same plain-HTTP
# file as before, so a certificate that is not trusted yet cannot make a
# working name look broken — that is a separate problem with a separate fix.
try {
    $viaName = (Invoke-WebRequest -Uri "http://$Name/fanzart-ca.pem" `
                    -UseBasicParsing -TimeoutSec 10).Content
    if ($viaName -match "BEGIN CERTIFICATE") {
        Good "http://$Name reaches the server"
    }
} catch {
    Warn "the name resolves but http://$Name did not answer — check a firewall"
}

Write-Host @"

    ------------------------------------------------------------------
    Done. Open  https://$Name

    If the browser now says the connection is NOT SECURE, that is the
    certificate, not the name, and it is a separate one-time step:

        .\trust-ca-windows.ps1

    If Kaspersky says "visiting an untrustworthy website has been
    prevented", the server also has to be added to its trusted
    addresses - see OFFICE-MACHINES.md.

    This machine now has the address written down. If the server is
    ever given a different IP, re-run this script.
    ------------------------------------------------------------------

"@ -ForegroundColor Green
