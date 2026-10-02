# 网络不稳时的推送脚本：失败自动重试（指数退避），默认最多 8 次。
# 用法： .\push.ps1            仅推送 master
#       .\push.ps1 -Tags      推送 master 并附带所有标签
param(
    [switch]$Tags,
    [int]$MaxRetry = 8
)

$ErrorActionPreference = 'Continue'
$delay = 3

for ($i = 1; $i -le $MaxRetry; $i++) {
    Write-Host "=== 推送尝试 $i / $MaxRetry ===" -ForegroundColor Cyan

    if ($Tags) {
        git push origin master --tags 2>&1 | ForEach-Object { Write-Host $_ }
    }
    else {
        git push origin master 2>&1 | ForEach-Object { Write-Host $_ }
    }

    if ($LASTEXITCODE -eq 0) {
        Write-Host "✅ 推送成功（第 $i 次尝试）" -ForegroundColor Green
        git log --oneline -1
        exit 0
    }

    $msg = "第 $i 次失败，${delay}s 后重试…"
    if ((git rev-parse HEAD) -and (git status -sb) -match 'ahead') {
        $msg = "第 $i 次失败（本地仍有未推送提交），${delay}s 后重试…"
    }
    Write-Host $msg -ForegroundColor Yellow
    Start-Sleep -Seconds $delay
    $delay = [Math]::Min($delay * 2, 30)   # 退避上限 30s
}

Write-Host "❌ 已重试 $MaxRetry 次仍失败，请检查网络/代理后再试" -ForegroundColor Red
exit 1
