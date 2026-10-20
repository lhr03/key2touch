#Requires -Version 5.1
<#
    key2touch 打包脚本 —— 生成 dist\key2touch.exe（单文件、免安装）

    用法（在本目录下）：
        powershell -ExecutionPolicy Bypass -File .\build.ps1

    说明：
      * 会在本目录建一个 .venv 虚拟环境，不污染系统 Python
      * 需要一个带 tkinter 的 Python 3.9+（官方安装包默认包含 tkinter）
#>

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

Write-Host '[1/5] 查找带 tkinter 的 Python ...' -ForegroundColor Cyan

# ---- 找一个能 import tkinter 的 Python ----
$pyExe = $null
$pyPre = @()
$candidates = @(
    @{ exe = 'python';  args = @() },
    @{ exe = 'py';      args = @('-3') },
    @{ exe = 'python3'; args = @() }
)

foreach ($c in $candidates) {
    try {
        $null = & $c.exe @($c.args) -c "import tkinter" 2>&1
        if ($LASTEXITCODE -eq 0) {
            $pyExe = $c.exe
            $pyPre = $c.args
            break
        }
    } catch {
        continue
    }
}

if (-not $pyExe) {
    Write-Host ''
    Write-Host '找不到可用的 Python（或该 Python 缺少 tkinter）。' -ForegroundColor Red
    Write-Host '请安装 Python 3.9+ 官方版：https://www.python.org/downloads/windows/' -ForegroundColor Yellow
    Write-Host '安装时务必勾选 "tcl/tk and IDLE"（默认已勾选）。' -ForegroundColor Yellow
    exit 1
}

$ver = & $pyExe @pyPre -c "import sys; print(sys.version.split()[0])"
Write-Host ("      使用 Python {0}（{1}）" -f $ver, $pyExe) -ForegroundColor Gray

# ---- 虚拟环境 ----
if (-not (Test-Path '.venv')) {
    Write-Host '[2/5] 创建虚拟环境 .venv ...' -ForegroundColor Cyan
    & $pyExe @pyPre -m venv .venv
} else {
    Write-Host '[2/5] 复用已有虚拟环境 .venv' -ForegroundColor Cyan
}

$vpy = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $vpy)) {
    Write-Host '虚拟环境创建失败（.venv\Scripts\python.exe 不存在）。' -ForegroundColor Red
    exit 1
}

Write-Host '[3/5] 安装打包依赖 PyInstaller ...' -ForegroundColor Cyan
& $vpy -m pip install --upgrade pip --quiet
& $vpy -m pip install -r requirements-dev.txt --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Host 'PyInstaller 安装失败，请检查网络。' -ForegroundColor Red
    exit 1
}

Write-Host '[4/5] 打包（单文件、无控制台窗口）...' -ForegroundColor Cyan
& $vpy -m PyInstaller --onefile --noconsole --name key2touch --clean --noconfirm key2touch.py
if ($LASTEXITCODE -ne 0) {
    Write-Host '打包失败。' -ForegroundColor Red
    exit 1
}

Write-Host '[5/5] 完成' -ForegroundColor Cyan
$exe = Join-Path $PSScriptRoot 'dist\key2touch.exe'
if (Test-Path $exe) {
    $mb = (Get-Item $exe).Length / 1MB
    Write-Host ''
    Write-Host ('  产物: dist\key2touch.exe  ({0:N1} MB)' -f $mb) -ForegroundColor Green
    Write-Host '  直接双击运行即可，无需安装。' -ForegroundColor Green
} else {
    Write-Host '未找到 dist\key2touch.exe，请检查上面的输出。' -ForegroundColor Red
    exit 1
}
