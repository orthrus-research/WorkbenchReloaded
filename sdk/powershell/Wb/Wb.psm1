# Workbench Reloaded for PowerShell (D24): token, submit, long-poll, envelope.
#   Import-Module .\sdk\powershell\Wb
#   $r = Invoke-Wb selftest.echo @{ text = 'ps' }          # waits; returns a WbEnvelope
#   $job = Invoke-Wb frames.compare @{ reference = $ref; candidate = $cand } -Detach
#   Wait-WbJob $job.job
# The endpoint and token come from WB_URL and WB_TOKEN (or WB_TOKEN_FILE), else from ~\.config\wb\run.

. (Join-Path $PSScriptRoot 'WbEnvelope.ps1')

function Get-WbEndpoint {
    $run = Join-Path ($env:WB_CONFIG ?? (Join-Path $HOME '.config\wb')) 'run'
    $url = $env:WB_URL
    if (-not $url -and (Test-Path (Join-Path $run 'endpoint.json'))) {
        $url = (Get-Content (Join-Path $run 'endpoint.json') -Raw | ConvertFrom-Json).url
    }
    $token = $env:WB_TOKEN
    if (-not $token) {
        $file = $env:WB_TOKEN_FILE ?? (Join-Path $run 'token')
        if (Test-Path $file) { $token = (Get-Content $file -Raw).Trim() }
    }
    [pscustomobject]@{ Url = ($url ?? 'http://127.0.0.1:7377').TrimEnd('/'); Token = $token }
}

function Invoke-WbApi {
    param([string]$Method, [string]$Path, $Body, [hashtable]$Headers = @{}, [int]$TimeoutSec = 90)
    $endpoint = Get-WbEndpoint
    $all = @{ Accept = 'application/json' } + $Headers
    if ($endpoint.Token) { $all.Authorization = "Bearer $($endpoint.Token)" }
    $arguments = @{ Method = $Method; Uri = "$($endpoint.Url)$Path"; Headers = $all; TimeoutSec = $TimeoutSec
                    SkipHttpErrorCheck = $true; StatusCodeVariable = 'status' }
    if ($null -ne $Body) {
        $arguments.Body = ($Body | ConvertTo-Json -Depth 20 -Compress)
        $arguments.ContentType = 'application/json'
    }
    $response = Invoke-RestMethod @arguments
    if ($status -ge 400) {
        $message = if ($response.error) { $response.error } else { "HTTP $status" }
        $exception = [System.Exception]::new("wb: $message")
        $exception.Data['status'] = $status
        throw $exception
    }
    $response
}

function Get-WbCommand {
    param([string]$Name)
    if ($Name) { Invoke-WbApi GET "/v1/commands/$Name" } else { (Invoke-WbApi GET '/v1/commands').commands }
}

function Get-WbJob {
    param([Parameter(Mandatory)][string]$Job, [int]$Wait = 0)
    $query = if ($Wait -gt 0) { "?wait=$Wait" } else { '' }
    [WbEnvelope]::new((Invoke-WbApi GET "/v1/jobs/$Job$query" -TimeoutSec ($Wait + 60)))
}

function Wait-WbJob {
    param([Parameter(Mandatory)][string]$Job, [int]$TimeoutSec = 0)
    $deadline = if ($TimeoutSec -gt 0) { (Get-Date).AddSeconds($TimeoutSec) } else { [datetime]::MaxValue }
    $envelope = Get-WbJob $Job
    while ($envelope.state -notin @('settled', 'cancelled', 'interrupted', 'awaiting_approval')) {
        $remaining = [int][Math]::Min(60, ($deadline - (Get-Date)).TotalSeconds)
        if ($remaining -le 0) { break }
        $envelope = Get-WbJob $Job -Wait ([Math]::Max(1, $remaining))
    }
    $envelope
}

function Invoke-Wb {
    <#
    .SYNOPSIS
    Runs a Workbench command and returns its wb/result/v1 envelope (or, with -Detach, at once).
    A command that spends money returns in state awaiting_approval with approval.prompt and approval.quote;
    approve with Approve-WbJob only after an explicit yes.
    #>
    param([Parameter(Mandatory, Position = 0)][string]$Command, [Parameter(Position = 1)][hashtable]$Params = @{},
          [string]$Out, [switch]$Fresh, [switch]$Detach, [int]$TimeoutSec = 0)
    $body = @{ command = $Command; params = $Params; fresh = [bool]$Fresh; path_style = 'windows' }
    if ($Out) { $body.out = (Resolve-Path -LiteralPath $Out -ErrorAction SilentlyContinue)?.Path ?? $Out }
    $created = Invoke-WbApi POST '/v1/jobs' $body @{ 'Idempotency-Key' = [guid]::NewGuid().ToString() }
    if ($Detach) { return [WbEnvelope]::new($created) }
    Wait-WbJob $created.job -TimeoutSec $TimeoutSec
}

function Approve-WbJob {
    param([Parameter(Mandatory)][string]$Job, [Parameter(Mandatory)][string]$Quote,
          [ValidateSet('direct', 'relayed')][string]$Via = 'direct')
    [WbEnvelope]::new((Invoke-WbApi POST "/v1/jobs/$Job/approve" @{ quote = $Quote; via = $Via }))
}

function Stop-WbJob {
    param([Parameter(Mandatory)][string]$Job)
    [WbEnvelope]::new((Invoke-WbApi POST "/v1/jobs/$Job/cancel" @{}))
}

Export-ModuleMember -Function Get-WbEndpoint, Get-WbCommand, Get-WbJob, Wait-WbJob, Invoke-Wb, Approve-WbJob, Stop-WbJob
