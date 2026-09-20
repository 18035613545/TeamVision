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
# dist-012（2026-09-20）：待签清单里**点名**的文件不存在即算失败（exit 1），不再静默跳过；
# 只有通配条目（默认清单里的 dist\TeamVision-Setup-*.exe，安装包还没打时本就可以没有）
# 才记"跳过"，且 -Release 下连通配未匹配也算失败——避免未签名产物带着 exit 0 出门。
# 注意：自签证书只能让**本机**验签通过，在别人的机器上仍然是「未知发布者」，
# 对反作弊/杀软白名单没有任何帮助——它只是让本地测试时不再出现未签名提示。

[CmdletBinding()]
param(
    # 第 131 条：打包形态改为 onedir，两个 exe 分别位于 dist\fps-host\ 与 dist\fps-viewer\
    # dist-012（2026-09-20）：安装包条目改为通配。旧实现写死 'dist\TeamVision-Setup-1.4.0.exe'，
    # 版本 bump 时只要漏改这一行，文件不存在就只记一次"跳过"、脚本仍 exit 0 —— 未签名的
    # 对外产物会带着成功退出码出门。通配后文件名随 installer.iss 的 OutputBaseFilename 走。
    [string[]]$Files = @(
        'dist\fps-host\fps-host.exe',
        'dist\fps-viewer\fps-viewer.exe',
        'dist\TeamVision-Setup-*.exe'
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
        # dist-012（2026-09-20）：待签文件不存在**必须算失败**。旧实现只打一行 [跳过] 并
        # 返回 $null，主流程记 skip++ 而不计 fail，脚本最后仍以 exit 0 收尾——未签名的对外
        # 产物会带着"成功"退出码出门（版本 bump 漏改清单时正是这条路径）。
        # "产物本就可以还没有"的情形（尚未打安装包）已由主流程的通配条目单独处理，
        # 走到这里的都是调用方点名要签的文件。
        throw "待签文件不存在: $path"
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
    # dist-012 附带：UnknownError 只在**本机自签**时才可放行——自签证书不进他人信任链是预期。
    # 旧实现对任何证书都把 UnknownError 当成功，于是 CA 证书出现 UnknownError（时间戳服务
    # 不可达、CRL/OCSP 拉不到等）同样被静默放过，对外产物带着验不过的签名出门。
    $okStatuses = @('Valid')
    if ($cert.Subject -eq $cert.Issuer) { $okStatuses += 'UnknownError' }
    $result = Set-AuthenticodeSignature @splat
    if ($result.Status -in $okStatuses) {
        # UnknownError 常见于自签未加入信任链；签名本身已写入
        Write-Host "    Status=$($result.Status)  $($result.StatusMessage)"
    } elseif ($result.Status -eq 'NotSigned') {
        throw "签名失败（仍为 NotSigned）: $($result.StatusMessage)"
    } else {
        Write-Host "    Status=$($result.Status)  $($result.StatusMessage)" -ForegroundColor Yellow
    }

    # 第 132 条（2026-09-11）：签名后必须**复核文件本身**，不能只看 Set-AuthenticodeSignature
    # 的返回值。实测在 140MB 的安装包上带时间戳签名会得到 HashMismatch——签名块已写入，
    # 但文件哈希与签名不符，而调用方按返回状态判为成功、静默收尾；用户拿到的是验签失败、
    # 甚至会被 SmartScreen/杀软拦下的安装包。这里复核一次，失败则去掉时间戳重试。
    $verify = Get-AuthenticodeSignature -FilePath $path
    if ($verify.Status -ne 'Valid' -and $splat.ContainsKey('TimestampServer')) {
        Write-Host "    复核结果为 $($verify.Status)，改为不带时间戳重签…" -ForegroundColor Yellow
        $splat.Remove('TimestampServer')
        Set-AuthenticodeSignature @splat | Out-Null
        $verify = Get-AuthenticodeSignature -FilePath $path
    }
    if ($verify.Status -eq 'NotSigned') {
        throw "签名复核失败（仍为 NotSigned）: $($verify.StatusMessage)"
    }
    if ($verify.Status -in $okStatuses) {
        Write-Host "    复核 Status=$($verify.Status)  $($verify.StatusMessage)"
        return $true
    }
    Write-Host "    复核 Status=$($verify.Status)  $($verify.StatusMessage)" -ForegroundColor Red
    return $false
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
    # dist-012：条目分两类——
    #   含通配符（如 dist\TeamVision-Setup-*.exe）：产物"本就可以还没构建"，展开后逐个签；
    #     一条都没匹配上才算跳过。-Release 是对外发布，没匹配到安装包直接判失败。
    #   显式路径（含调用方 -Files 传进来的）：不存在就是失败，见 Invoke-SignOne。
    if ($f -match '[\*\?]') {
        $hits = @(Get-ChildItem -Path (Join-Path $root $f) -ErrorAction SilentlyContinue |
                  Sort-Object LastWriteTime -Descending)
        if ($hits.Count -eq 0) {
            if ($Release) {
                Write-Host "    [错误] -Release 模式下通配条目未匹配到任何文件: $f" -ForegroundColor Red
                $fail++
            } else {
                Write-Host "    [跳过] 通配未匹配到文件（该产物尚未构建）: $f" -ForegroundColor Yellow
                $skip++
            }
            continue
        }
        if ($hits.Count -gt 1) {
            Write-Host "    提示: $f 匹配到 $($hits.Count) 个文件，全部签名" -ForegroundColor Yellow
        }
        $targets = @($hits | ForEach-Object { $_.FullName })
    } else {
        $targets = @(Join-Path $root $f)
    }
    foreach ($full in $targets) {
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
}

Write-Host ""
Write-Host "签名完成: 成功 $ok / 失败 $fail / 跳过（通配未匹配）$skip" -ForegroundColor Green
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
