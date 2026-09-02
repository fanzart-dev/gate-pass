# Trust the gate pass server's certificate, on a Windows office machine.
#
#   Right-click -> Run with PowerShell        (as Administrator)
#
# or from an Administrator PowerShell:
#
#   Set-ExecutionPolicy -Scope Process Bypass -Force
#   .\trust-ca-windows.ps1
#
# Fetches the CA from the server and installs it into the machine's Trusted
# Root store, which is what Edge, Chrome and Brave read on Windows. Firefox
# keeps its own store and is handled separately at the end.
#
# NOT TESTED ON WINDOWS. It was written on Linux, where PowerShell is not
# available to run it, so treat the first run as the test. Every step prints
# what it did; if one fails, the manual equivalent is in the message.
#
# If Kaspersky (or any other antivirus that scans HTTPS) is installed, this
# script is not enough on its own — see the note it prints at the end.

param(
    [string]$Server = "fanzart-server.local",
    [string]$File   = ""
)

$ErrorActionPreference = "Stop"
function Say  ($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Note ($m) { Write-Host "    $m" }
function Fail ($m) { Write-Host "`nERROR: $m" -ForegroundColor Red; exit 1 }

# Installing into the machine-wide root store needs Administrator. Without it
# the certificate can only go into the current user's store, which leaves every
# other account on the machine still warning.
$admin = ([Security.Principal.WindowsPrincipal] `
          [Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Fail "run this as Administrator (right-click PowerShell -> Run as administrator)"
}

Say "Getting the certificate"
if ($File -ne "") {
    if (-not (Test-Path $File)) { Fail "no such file: $File" }
    $path = $File
    Note "using $path"
} else {
    $path = Join-Path $env:TEMP "fanzart-ca.pem"
    # Plain HTTP on purpose: this is the file that MAKES https trustworthy, so
    # it cannot require a trusted https connection to fetch.
    try {
        Invoke-WebRequest -Uri "http://$Server/fanzart-ca.pem" -OutFile $path `
                          -UseBasicParsing -TimeoutSec 20
    } catch {
        Fail "could not fetch http://$Server/fanzart-ca.pem`n       $($_.Exception.Message)`n       Pass -Server <address> if the server is elsewhere, or -File <path>."
    }
    Note "fetched from http://$Server/fanzart-ca.pem"
}

$cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2 $path
Note "subject:     $($cert.Subject)"
Note "expires:     $($cert.NotAfter)"
Note "fingerprint: $($cert.Thumbprint)"
# Check it against the fingerprint the server printed. If they differ, stop:
# something is serving you a different certificate than the one you expect.

Say "Windows Trusted Root store (Edge, Chrome, Brave)"
try {
    $store = New-Object System.Security.Cryptography.X509Certificates.X509Store `
             "Root", "LocalMachine"
    $store.Open("ReadWrite")
    $existing = $store.Certificates | Where-Object { $_.Thumbprint -eq $cert.Thumbprint }
    if ($existing) {
        Note "already installed"
    } else {
        $store.Add($cert)
        Note "installed"
    }
    $store.Close()
} catch {
    Fail "could not write to the root store: $($_.Exception.Message)`n       Manual: double-click the .pem -> Install Certificate -> Local Machine`n       -> Place all certificates in the following store -> Trusted Root Certification Authorities"
}

Say "Checking"
try {
    Invoke-WebRequest -Uri "https://$Server/login" -UseBasicParsing -TimeoutSec 15 | Out-Null
    Note "https://$Server/login verifies"
} catch {
    Note "https://$Server/login still does not verify:"
    Note "  $($_.Exception.Message)"
    Note "  If antivirus scans HTTPS on this machine, see the note below."
}

Write-Host @"

Firefox keeps its own certificate store and ignores the one above:
    Settings -> Privacy & Security -> Certificates -> View Certificates
    -> Authorities -> Import -> pick $path -> tick "identify websites"

IF KASPERSKY (OR SIMILAR) IS INSTALLED, THIS IS NOT ENOUGH.

Kaspersky inspects HTTPS by intercepting it: it terminates the connection
itself, checks the site's certificate against ITS OWN list of authorities, and
re-encrypts with a certificate of its own. Windows' Trusted Root store is not
that list, so installing the CA there does not make Kaspersky accept it — the
browser is trusting Kaspersky, and Kaspersky is refusing the server.

Two ways out, in order of preference:

  1. Add the server to Kaspersky's trusted addresses, so it stops inspecting
     that one site. Roughly:
        Settings -> Network settings (or Network) -> Encrypted connection
        scanning -> Trusted addresses / Manage exclusions -> add
            fanzart-server.local
     The exact wording moves between Kaspersky versions and between the home
     and business products; look for "encrypted connection" and "trusted" or
     "exclusions".

  2. Turn off encrypted connection scanning entirely. This works, but it stops
     Kaspersky inspecting every HTTPS site on the machine, not just this one.
     Prefer option 1.

If neither is available to you — a locked-down corporate Kaspersky policy, for
instance — then use plain HTTP on that machine:

    http://$Server

The traffic stays on the office LAN and never crosses the internet. That is a
reasonable position; it is where this server was until today.
"@
