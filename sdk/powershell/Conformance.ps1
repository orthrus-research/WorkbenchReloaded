# The shared conformance checks against a live daemon (tests/test_sdks.py starts one and sets WB_URL, WB_TOKEN).
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'Wb') -Force
function Check($condition, $what) { if (-not $condition) { throw "conformance failed: $what" } }
$echo = Invoke-Wb selftest.echo @{ text = 'ps'; times = 2 } -TimeoutSec 60
Check ($echo.schema -eq 'wb/result/v1') 'schema'
Check ($echo.outcome -eq 'ok' -and $echo.ExitCode() -eq 0) "echo ok: $($echo.summary)"
Check ($echo.data.text -eq 'psps') 'typed data'
$finding = Invoke-Wb selftest.fail @{ outcome = 'finding' } -TimeoutSec 60
Check ($finding.code -eq 1) 'finding code'
$usage = Invoke-Wb selftest.echo @{ times = 0 } -TimeoutSec 60
Check ($usage.outcome -eq 'usage') 'usage'
$spend = Invoke-Wb selftest.spend @{ price = 0 } -TimeoutSec 60
Check ($spend.state -eq 'awaiting_approval' -and $spend.ExitCode() -eq 6) 'awaiting approval'
Check ($spend.approval.prompt.question) 'prompt'
$null = Approve-WbJob $spend.job $spend.approval.quote.id -Via relayed
$done = Wait-WbJob $spend.job -TimeoutSec 60
Check ($done.outcome -eq 'ok') 'approved spend settles ok'
try { $null = Invoke-Wb selftest.nothing; Check $false 'expected a 404' } catch { Check ($_.Exception.Data['status'] -eq 404) 'unknown command is 404' }
'powershell conformance: ok'
