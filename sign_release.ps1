# Signs dist\VamarenStockChecker.exe with a code-signing certificate.
#
# Usage:
#   .\sign_release.ps1                  -> uses the best code-signing cert
#                                          found in your personal store
#   .\sign_release.ps1 -Thumbprint XX   -> uses a specific certificate
#   .\sign_release.ps1 -CreateSelfSigned -> makes a self-signed cert first
#                                           (testing only: the public will
#                                           still see 'Unknown Publisher')
#
# When you purchase a real certificate (Azure Trusted Signing, Certum,
# SSL.com, etc.), install it per the vendor's instructions and run this
# script - no changes needed.

param(
    [string]$Thumbprint = "",
    [switch]$CreateSelfSigned
)

$exe = Join-Path $PSScriptRoot "dist\VamarenStockChecker.exe"
if (-not (Test-Path $exe)) { Write-Error "Build the exe first."; exit 1 }

if ($CreateSelfSigned) {
    $cert = New-SelfSignedCertificate -Type CodeSigningCert `
        -Subject "CN=JohnEugeo, O=Vamaren Stock Checker" `
        -CertStoreLocation Cert:\CurrentUser\My `
        -NotAfter (Get-Date).AddYears(3)
    Write-Output "Created self-signed cert: $($cert.Thumbprint)"
} elseif ($Thumbprint) {
    $cert = Get-Item "Cert:\CurrentUser\My\$Thumbprint" -ErrorAction Stop
} else {
    $cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert |
        Sort-Object NotAfter -Descending | Select-Object -First 1
    if (-not $cert) {
        Write-Error ("No code-signing certificate found. Get one " +
                     "(Azure Trusted Signing / Certum / SSL.com) or run " +
                     "with -CreateSelfSigned for local testing.")
        exit 1
    }
}

Write-Output "Signing with: $($cert.Subject)  [$($cert.Thumbprint)]"
$result = Set-AuthenticodeSignature -FilePath $exe -Certificate $cert `
    -HashAlgorithm SHA256 -TimestampServer "http://timestamp.digicert.com"
Write-Output "Status : $($result.Status)"
Write-Output "Signer : $($result.SignerCertificate.Subject)"
if ($result.Status -eq "Valid") {
    Write-Output "Signature is fully trusted on this machine."
} elseif ($result.SignerCertificate) {
    Write-Output ("Signed, but the certificate is not publicly trusted " +
                  "(expected for self-signed).")
}
