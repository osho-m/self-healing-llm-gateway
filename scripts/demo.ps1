# End-to-end walkthrough against a running stack (docker compose up, or uvicorn locally).
#   .\scripts\demo.ps1 [-BaseUrl http://localhost:8000] [-AdminKey demo-admin-key-change-me-0000]
param(
    [string]$BaseUrl = "http://localhost:8000",
    [string]$AdminKey = $(if ($env:GATEWAY_ADMIN_API_KEY) { $env:GATEWAY_ADMIN_API_KEY } else { "demo-admin-key-change-me-0000" })
)
$ErrorActionPreference = "Stop"
$team = @{ "X-API-Key" = "demo-live-key" }
$admin = @{ "X-Admin-Key" = $AdminKey }

function Send-Chat([string]$Key = "demo-live-key", [string]$Tier = "balanced") {
    $body = @{ model = $Tier; messages = @(@{ role = "user"; content = "Explain circuit breakers." }) } | ConvertTo-Json -Depth 5
    try {
        $r = Invoke-RestMethod -Method Post -Uri "$BaseUrl/v1/chat/completions" -Headers @{ "X-API-Key" = $Key } -ContentType "application/json" -Body $body
        "{0,-15} fallback={1,-5} attempts=[{2}]" -f $r.x_gateway.provider, $r.x_gateway.fallback_used, ($r.x_gateway.attempts -join "; ")
    } catch {
        "HTTP {0}: {1}" -f $_.Exception.Response.StatusCode.value__, $_.ErrorDetails.Message
    }
}

Write-Host "`n== 1. Normal request (served by the tier's primary)" -ForegroundColor Cyan
Send-Chat

Write-Host "`n== 2. Simulate mock-primary outage -> retries, circuit opens, failover" -ForegroundColor Cyan
Invoke-RestMethod -Method Post -Uri "$BaseUrl/v1/admin/providers/mock-primary/simulate-outage?enabled=true" -Headers $admin | Out-Null
1..3 | ForEach-Object { Send-Chat }
(Invoke-RestMethod -Uri "$BaseUrl/v1/admin/providers" -Headers $admin).PSObject.Properties |
    ForEach-Object { "{0,-15} status={1,-9} circuit={2}" -f $_.Name, $_.Value.status, $_.Value.circuit }

Write-Host "`n== 3. Restore; after the cooldown a single half-open probe closes the circuit" -ForegroundColor Cyan
Invoke-RestMethod -Method Post -Uri "$BaseUrl/v1/admin/providers/mock-primary/simulate-outage?enabled=false" -Headers $admin | Out-Null
$cooldown = 16
Write-Host "waiting $cooldown s for cooldown..."
Start-Sleep -Seconds $cooldown
Send-Chat

Write-Host "`n== 4. Rate limiting a high-volume team (search: 30 req/min)" -ForegroundColor Cyan
$codes = 1..40 | ForEach-Object {
    try { Invoke-WebRequest -UseBasicParsing -Method Post -Uri "$BaseUrl/v1/chat/completions" -Headers @{ "X-API-Key" = "search-live-key" } `
            -ContentType "application/json" -Body '{"model":"economy","messages":[{"role":"user","content":"hi"}]}' | Out-Null; 200 }
    catch { $_.Exception.Response.StatusCode.value__ }
}
$codes | Group-Object | ForEach-Object { "HTTP {0}: {1}" -f $_.Name, $_.Count }

Write-Host "`n== 5. Team budget status and a runtime limit change (audited)" -ForegroundColor Cyan
Invoke-RestMethod -Uri "$BaseUrl/v1/admin/teams/demo/status" -Headers $admin | ConvertTo-Json -Depth 4
Invoke-RestMethod -Method Patch -Uri "$BaseUrl/v1/admin/teams/search" -Headers $admin -ContentType "application/json" `
    -Body '{"requests_per_minute": 120}' | Select-Object team_id, limits, runtime_overrides | ConvertTo-Json -Depth 4
Invoke-RestMethod -Uri "$BaseUrl/v1/admin/audit?limit=3" -Headers $admin | ConvertTo-Json -Depth 4

Write-Host "`nGrafana: http://localhost:3000  Prometheus: http://localhost:9090  Jaeger: http://localhost:16686" -ForegroundColor Green
