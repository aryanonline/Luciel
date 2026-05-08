<#
.SYNOPSIS
  Load Luciel dev secrets from the Windows Credential Manager into the
  current PowerShell session's environment variables.

.DESCRIPTION
  P3-P fix (2026-05-06, Step 28 C11.a). Replaces the previous habit of
  copy-pasting LUCIEL_PLATFORM_ADMIN_KEY (and other dev secrets) from a
  Notepad window into PowerShell. That pattern leaked the key into shell
  history, screenshot capture, and cross-context paste buffers.

  Backing store is the Windows Credential Manager (Generic credential
  type). On first run the script prompts for any missing secrets, writes
  them into Credential Manager, and then loads them into the current
  session. Subsequent runs read straight from Credential Manager with no
  prompt.

  Each secret is identified by a Target name of the shape:
      Luciel:dev:<env-var-name>
  e.g. Luciel:dev:LUCIEL_PLATFORM_ADMIN_KEY. The Target name is the only
  identifier persisted to disk; the secret value is encrypted by DPAPI
  scoped to the current Windows user. No cross-user readability.

.PARAMETER Refresh
  If specified, prompts for every secret regardless of whether it is
  already stored in Credential Manager. Use this after rotating a dev
  key to overwrite the stored value.

.PARAMETER List
  If specified, lists the Target names this script knows about and
  whether each one is currently present in Credential Manager. Does NOT
  print secret values.

.NOTES
  Why Credential Manager and not a flat encrypted file:
  - DPAPI integration is automatic. No password to remember, no key file
    to back up, no rotation cadence to manage.
  - Scope is the Windows user account. Anyone who can already log in as
    aryan can already read the stored secret; the threat model treats
    Windows account compromise as out of scope (separate concern from
    "key sitting in a Notepad window the operator forgets to close").
  - cmdkey is on every Windows install since Vista. No third-party
    tooling, no winget dependency, no Python wrapper.

  Why Generic credential type and not Domain credentials:
  - Generic credentials have no implicit network use. Storing them does
    NOT make Windows attempt to use them for SMB/HTTP/etc.

  This script is dev-only. Production secrets live in KMS-encrypted SSM
  parameters and are read by ECS task roles via the ECS-managed env var
  injection -- the operator never holds a copy.

.EXAMPLE
  .\scripts\load-dev-secrets.ps1
  # On first run: prompts for any missing dev secrets and stores them.
  # On later runs: silently exports them into the current session.

.EXAMPLE
  .\scripts\load-dev-secrets.ps1 -List
  # Shows which secrets are stored, without revealing values.

.EXAMPLE
  .\scripts\load-dev-secrets.ps1 -Refresh
  # Overwrites all stored secrets with freshly prompted values.
#>

[CmdletBinding()]
param(
    [switch]$Refresh,
    [switch]$List
)

$ErrorActionPreference = "Stop"

# ----- Catalogue of dev secrets this script manages -----
# Add a new entry by appending to this array. Each item is the env var
# name; the Target name is constructed automatically.
$DevSecretEnvVars = @(
    "LUCIEL_PLATFORM_ADMIN_KEY"
)

function Get-TargetName {
    param([string]$EnvVarName)
    return "Luciel:dev:$EnvVarName"
}

function Test-StoredCredential {
    param([string]$Target)
    # cmdkey /list:<target> exits 0 with output if found, exits 0 with
    # "Currently stored credentials: NONE" message if not found. The
    # exit code is unreliable; parse stdout instead.
    $output = cmdkey /list:$Target 2>&1 | Out-String
    return $output -match [regex]::Escape("Target: $Target")
}

function Read-StoredCredential {
    param([string]$Target)
    # cmdkey does NOT print the secret value (by design). To read the
    # actual value we use the Windows API via .NET P/Invoke. The
    # CredRead function returns a CREDENTIAL struct whose CredentialBlob
    # field holds the secret as UTF-16 bytes.
    $code = @'
using System;
using System.Runtime.InteropServices;
using System.Text;

public static class LucielCredManager {
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    public struct CREDENTIAL {
        public uint Flags;
        public uint Type;
        public IntPtr TargetName;
        public IntPtr Comment;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
        public uint CredentialBlobSize;
        public IntPtr CredentialBlob;
        public uint Persist;
        public uint AttributeCount;
        public IntPtr Attributes;
        public IntPtr TargetAlias;
        public IntPtr UserName;
    }

    [DllImport("Advapi32.dll", SetLastError=true, EntryPoint="CredReadW", CharSet=CharSet.Unicode)]
    public static extern bool CredRead(string target, uint type, uint reservedFlag, out IntPtr credentialPtr);

    [DllImport("Advapi32.dll", SetLastError=true, EntryPoint="CredFree")]
    public static extern void CredFree(IntPtr buffer);

    public static string Read(string target) {
        IntPtr credPtr;
        // type 1 = CRED_TYPE_GENERIC
        if (!CredRead(target, 1, 0, out credPtr)) {
            int err = Marshal.GetLastWin32Error();
            throw new System.ComponentModel.Win32Exception(err);
        }
        try {
            CREDENTIAL cred = (CREDENTIAL)Marshal.PtrToStructure(credPtr, typeof(CREDENTIAL));
            byte[] blob = new byte[cred.CredentialBlobSize];
            Marshal.Copy(cred.CredentialBlob, blob, 0, (int)cred.CredentialBlobSize);
            // Stored as UTF-16 (2 bytes per char) by cmdkey.
            return Encoding.Unicode.GetString(blob);
        } finally {
            CredFree(credPtr);
        }
    }
}
'@
    if (-not ([System.Management.Automation.PSTypeName]'LucielCredManager').Type) {
        Add-Type -TypeDefinition $code -Language CSharp
    }
    return [LucielCredManager]::Read($Target)
}

function Set-StoredCredential {
    param(
        [string]$Target,
        [System.Security.SecureString]$SecureValue
    )
    # Materialise the SecureString just long enough to hand it to
    # cmdkey, then zero the BSTR. cmdkey reads from the command line
    # -- it cannot read from stdin -- so we cannot avoid the plaintext
    # crossing the argument array. Mitigation: we hand it to a freshly
    # spawned cmdkey.exe (no shell history), and the BSTR is zeroed
    # immediately after.
    $bstr = [IntPtr]::Zero
    $plain = $null
    try {
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureValue)
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
        # /generic ensures Windows does not try to use this credential
        # for any implicit network operation. /pass takes the secret.
        $null = cmdkey /generic:$Target /user:luciel-dev /pass:$plain
        if ($LASTEXITCODE -ne 0) {
            throw "cmdkey failed to store credential for target $Target (exit $LASTEXITCODE)."
        }
    } finally {
        if ($bstr -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        }
        $plain = $null
    }
}

# ----- Mode: List -----
if ($List) {
    Write-Host ""
    Write-Host "Luciel dev secrets registry" -ForegroundColor Cyan
    Write-Host "============================"
    foreach ($var in $DevSecretEnvVars) {
        $target = Get-TargetName $var
        $present = Test-StoredCredential $target
        $status = if ($present) { "STORED  " } else { "MISSING " }
        $color = if ($present) { "Green" } else { "Yellow" }
        Write-Host "  [$status] $target" -ForegroundColor $color
    }
    Write-Host ""
    Write-Host "Use without -List to load stored secrets into this session."
    Write-Host "Use -Refresh to overwrite stored values with freshly prompted ones."
    return
}

# ----- Mode: load (default) and refresh -----
$loaded = 0
$prompted = 0
foreach ($var in $DevSecretEnvVars) {
    $target = Get-TargetName $var
    $needsPrompt = $Refresh -or (-not (Test-StoredCredential $target))

    if ($needsPrompt) {
        Write-Host "Storing $var (target=$target)" -ForegroundColor Yellow
        $secure = Read-Host -Prompt "  Enter value for $var" -AsSecureString
        if ($null -eq $secure -or $secure.Length -eq 0) {
            throw "$var value is empty; aborting (no partial load)."
        }
        Set-StoredCredential -Target $target -SecureValue $secure
        $secure.Dispose()
        $prompted++
    }

    # Read it back from Credential Manager and export into the session
    # env. Doing this even on the freshly-stored path means the load
    # path is the same code regardless of whether we just prompted.
    $value = Read-StoredCredential $target
    Set-Item -Path "Env:$var" -Value $value
    # Immediately drop the local plaintext reference.
    $value = $null
    $loaded++
}

Write-Host ""
Write-Host "load-dev-secrets.ps1 complete:" -ForegroundColor Green
Write-Host "  loaded into session env : $loaded secret(s)"
Write-Host "  prompted+stored this run: $prompted secret(s)"
Write-Host ""
Write-Host "These env vars are now set in THIS PowerShell session ONLY."
Write-Host "They do NOT persist to a new shell. Re-run this script in"
Write-Host "any new PowerShell window before running tooling that needs"
Write-Host "the secrets."
