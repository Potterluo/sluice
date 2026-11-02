# 启动本地 clef-flash 决策模型服务（llama.cpp systemone 端点）
# 用法：powershell -File tools/serve_clef.ps1 [-Port 18080] [-Gguf <path>] [-Server <path>]
param(
    [int]$Port = 18080,
    [string]$Gguf = "$env:USERPROFILE\models\Clef-Flash-Q4_K_M.gguf",
    [string]$Server = "$env:USERPROFILE\llamacpp\bin\llama-server.exe"
)

if (-not (Test-Path $Gguf)) {
    Write-Error "模型未下载: $Gguf（GGUF 见 https://huggingface.co/ggml-org/Clef-Flash-GGUF）"
    exit 1
}
if (-not (Test-Path $Server)) {
    Write-Error "llama-server 不存在: $Server（b12034+，含 /v1/systemone）"
    exit 1
}

Write-Host "启动 clef-flash (Q4_K_M) on port $Port ..."
# --systemone 启用 System One 决策端点 /v1/systemone（b12034+ 支持）
& $Server -m $Gguf --port $Port -c 4096 --systemone
