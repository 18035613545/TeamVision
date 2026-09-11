# TeamVision 代码签名脚本
# 用法：
#   powershell -ExecutionPolicy Bypass -File sign_exe.ps1
#   powershell -ExecutionPolicy Bypass -File sign_exe.ps1 -Files dist\fps-host\fps-host.exe
# 使用商业证书：
#   $env:TEAMVIEW_PFX = "C:\path\to\codesign.pfx"
#   $env:TEAMVIEW_PWD = "pfx-password"
#   powershell -ExecutionPolicy Bypass -File sign_exe.ps1
# 正式发布（对外分发）必须显式声明，且必须提供 CA 证书：
#   powershell -ExecutionPolicy Bypass -File sign_exe.ps1 -Release
# 无商业证书时（不带 -Release）：自动创建/复用本机 CurrentUser 自签证书 CN=TeamVision。
# 注意：自签证书只能让**本机**验签通过，在别人的机器上仍然是「未知发布者」，
# 对反作弊/杀软白名单没有任何帮助——它只是让本地测试时不再出现未签名提示。

[CmdletBinding()]
param(
    # 第 131 条：打包形态改为 onedir，两个 exe 分别位于 dist\fps-host\ 与 dist\fps-viewer\
    [string[]]$Files = @(
        'dist\fps-host\fps-host.exe',
        'dist\fps-viewer\fps-viewer.exe',
        'dist\TeamVision-Setup-1.3.0.exe'
    ),
    [string]$PfxPath = $env:TEAMVIEW_PFX,
    [string]$PfxPassword = $env:TEAMVIEW_PWD,
    [string]$SubjectCN = 'TeamVision',
    [string]$TimestampServer = 'http://timestamp.digicert.com',
    [switch]$SkipTimestamp,
    [switch]$TrustLocal,
    [switch]$Force,
    # 对外发布模式：必须是 CA 签发的证书，绝不回退到自签
    [switch]$Release
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Write-Step([string]$msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

function Get-CodeSigningCert {
    # 第 131 条：自签证书在别的机器上仍是「未知发布者」，无法进入反作弊/杀软白名单。
    # 对外发布时必须用 CA 签发的代码签名证书，因此 -Release 下拒绝静默回退到自签。
    if ($Release -and -not $PfxPath) {
        throw @"
-Release 模式需要 CA 签发的代码签名证书（OV/EV）。
请先设置 TEAMVIEW_PFX / TEAMVIEW_PWD 指向证书，或去掉 -Release 只做本地签名。
自签证书只能让本机验签通过，对外分发时与未签名没有区别。
"@
    }

    if ($PfxPath) {
        if (-not (Test-Path $PfxPath)) {
            throw "TEAMVIEW_PFX 指向的证书不存在: $PfxPath"
        }
        Write-Step "从 PFX 导入证书: $PfxPath"
        $secure = if ($PfxPassword) { ConvertTo-SecureString $PfxPassword -AsPlainText -Force } else { New-Object System.Security.SecureString }
        $certs = Import-PfxCertificate -FilePath $PfxPath -CertStoreLocation Cert:\CurrentUser\My -Password $secure
        return @($certs)[0]
    }

    $found = Get-ChildItem Cert:\CurrentUser\My |
        Where-Object {
            $_.Subject -match [regex]::Escape("CN=$SubjectCN") -and
            $_.HasPrivateKey -and
            ($_.EnhancedKeyUsageList.ObjectId -contains '1.3.6.1.5.5.7.3.3' -or
             $_.EnhancedKeyUsageList.Count -eq 0)
        } |
        Sort-Object NotAfter -Descending |
        Select-Object -First 1

    if ($found) {
        Write-Step "复用本机已有代码签名证书: $($found.Subject) [$($found.Thumbprint)]"
        return $found
    }

    Write-Step "未找到证书，创建自签代码签名证书 CN=$SubjectCN（CurrentUser\My）"
    Write-Host "    注意：自签证书只对**本机**有效；对外发布请改用 CA 代码签名证书，否则在其他机器上仍是「未知发布者」。" -ForegroundColor Yellow
    $cert = New-SelfSignedCertificate `
        -Type CodeSigningCert `
        -Subject "CN=$SubjectCN" `
        -CertStoreLocation Cert:\CurrentUser\My `
        -KeyExportPolicy Exportable `
        -KeyLength 2048 `
        -HashAlgorithm SHA256 `
        -NotAfter (Get-Date).AddYears(5)

    $pfxDir = Join-Path $root 'certs'
    if (-not (Test-Path $pfxDir)) { New-Item -ItemType Directory -Path $pfxDir | Out-Null }
    $pfxOut = Join-Path $pfxDir 'TeamVision-CodeSigning.pfx'
    $pwd = ConvertTo-SecureString -String 'TeamVision' -Force -AsPlainText
    Export-PfxCertificate -Cert $cert -FilePath $pfxOut -Password $pwd | Out-Null
    Export-Certificate -Cert $cert -FilePath (Join-Path $pfxDir 'TeamVision-CodeSigning.cer') | Out-Null
    Write-Host "    已导出: $pfxOut (密码: TeamVision) — 请勿提交到仓库；对外发布请改用 CA 证书。" -ForegroundColor Yellow
    return $cert
}

function Invoke-SignOne([string]$path, $cert) {
    if (-not (Test-Path $path)) {
        # 尚未构建的产物（例如还没跑 build_installer.bat 时的安装包）不算失败，
        # 否则「只签两个 exe」也会以退出码 1 收尾，调用方误以为签名整体失败。
        Write-Host "    [跳过] 文件不存在: $path" -ForegroundColor Yellow
        return $null
    }

    $current = Get-AuthenticodeSignature -FilePath $path
    if (-not $Force -and $current.Status -eq 'Valid' -and $current.SignerCertificate -and
        $current.SignerCertificate.Thumbprint -eq $cert.Thumbprint) {
        Write-Host "    [已是有效签名，跳过] $path"
        return $true
    }

    $splat = @{
        FilePath         = $path
        Certificate      = $cert
        HashAlgorithm    = 'SHA256'
        ErrorAction      = 'Stop'
    }
    if (-not $SkipTimestamp -and $TimestampServer) {
        $splat['TimestampServer'] = $TimestampServer
    }

    Write-Step "签名 $path"
    $result = Set-AuthenticodeSignature @splat
    if ($result.Status -in @('Valid', 'UnknownError')) {
        # UnknownError 常见于自签未加入信任链；签名本身已写入
        Write-Host "    Status=$($result.Status)  $($result.StatusMessage)"
        return $true
    }
    if ($result.Status -eq 'NotSigned') {
        throw "签名失败（仍为 NotSigned）: $($result.StatusMessage)"
    }
    Write-Host "    Status=$($result.Status)  $($result.StatusMessage)" -ForegroundColor Yellow
    return $true
}

# ---------- main ----------
$cert = Get-CodeSigningCert

if ($TrustLocal) {
    Write-Step "将证书加入本机受信任的根（仅本机验签通过；其他电脑不会自动信任）"
    $rootStore = New-Object System.Security.Cryptography.X509Certificates.X509Store('Root', 'CurrentUser')
    $rootStore.Open('ReadWrite')
    $rootStore.Add($cert)
    $rootStore.Close()
}

$ok = 0
$fail = 0
$skip = 0
foreach ($f in $Files) {
    $full = Join-Path $root $f
    try {
        $r = Invoke-SignOne $full $cert
        if ($null -eq $r) { $skip++ }
        elseif ($r) { $ok++ }
        else { $fail++ }
    } catch {
        Write-Host "    [错误] $_" -ForegroundColor Red
        $fail++
    }
}

Write-Host ""
Write-Host "签名完成: 成功 $ok / 失败 $fail / 跳过（不存在）$skip" -ForegroundColor Green
Get-ChildItem -Path $Files -ErrorAction SilentlyContinue | ForEach-Object {
    $sig = Get-AuthenticodeSignature $_.FullName
    Write-Host ("  {0,-40} {1}" -f $_.Name, $sig.Status)
}

# 第 131 条：明确区分「本机自签」与「CA 签发」。自签签名在别人机器上依然显示未知发布者，
# 对反作弊/杀软白名单没有任何作用——不要让它看起来像已经解决分发信任问题。
if ($cert.Subject -eq $cert.Issuer) {
    Write-Host ""
    Write-Host "注意：当前使用自签证书（$($cert.Subject)）。" -ForegroundColor Yellow
    Write-Host "      · 只有本机导入该证书后验签才通过；其他电脑上仍显示「未知发布者」。" -ForegroundColor Yellow
    Write-Host "      · 对杀软/反作弊的「未知发布者」判定没有任何改善。" -ForegroundColor Yellow
    Write-Host "      · 对外分发请用 CA 代码签名证书并加 -Release：" -ForegroundColor Yellow
    Write-Host '        $env:TEAMVIEW_PFX="codesign.pfx"; $env:TEAMVIEW_PWD="***"; .\sign_exe.ps1 -Release' -ForegroundColor Yellow
} else {
    Write-Host ""
    Write-Host "签名主体：$($cert.Subject)（由 CA 签发）" -ForegroundColor Green
}

if ($fail -gt 0) { exit 1 }
exit 0
