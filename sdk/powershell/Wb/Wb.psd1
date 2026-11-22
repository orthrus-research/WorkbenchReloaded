@{
    RootModule        = 'Wb.psm1'
    ModuleVersion     = '0.1.0'
    GUID              = '6c3c6f0e-5b1e-4b8e-9a52-2f2b7f1d9a41'
    Description       = 'Workbench Reloaded client: run commands, wait for their wb/result/v1 envelopes.'
    PowerShellVersion = '7.2'
    FunctionsToExport = @('Get-WbEndpoint', 'Get-WbCommand', 'Get-WbJob', 'Wait-WbJob', 'Invoke-Wb', 'Approve-WbJob', 'Stop-WbJob')
    ScriptsToProcess  = @('WbEnvelope.ps1')
}
