# generated from schema/envelope.schema.json by schema/generate.py; do not edit
class WbEnvelope {
    [string] $schema
    [string] $job
    [string] $command
    [string] $command_version
    [string] $module
    [string] $module_version
    [object] $params
    [string] $params_hash
    [string] $state
    [string] $phase
    [object] $waiting
    [string] $outcome
    [Nullable[long]] $code
    [string] $summary
    [object[]] $reasons
    [object[]] $inputs
    [object[]] $outputs
    [object] $data
    [object[]] $measurements
    [object[]] $findings
    [object] $machine
    [object] $toolchain
    [object] $timings
    [object] $cost
    [object] $cache
    [object] $approval
    [object[]] $children
    [string] $parent
    [object] $logs

    WbEnvelope([object] $raw) {
        foreach ($name in $raw.PSObject.Properties.Name) {
            if ($this.PSObject.Properties.Name -contains $name) { $this.$name = $raw.$name }
        }
    }

    [int] ExitCode() {
        if ($this.state -eq 'settled') { if ($null -eq $this.code) { return 3 } return [int]$this.code }
        if ($this.state -in @('cancelled', 'interrupted')) { return 4 }
        if ($this.state -eq 'awaiting_approval') { return 6 }
        return 5
    }
}
