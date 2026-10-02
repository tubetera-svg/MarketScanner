# Mirror the canonical agent skills (.claude/skills) into Kilo's skill path (.kilo/skill).
# Edit skills only under .claude/skills, then run:  powershell -File scripts/sync_agent_skills.ps1
$root = Split-Path -Parent $PSScriptRoot
$src = Join-Path $root ".claude\skills"
$dst = Join-Path $root ".kilo\skill"

Get-ChildItem -Path $src -Recurse -File | ForEach-Object {
    $target = Join-Path $dst $_.FullName.Substring($src.Length + 1)
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
    Copy-Item -LiteralPath $_.FullName -Destination $target -Force
}
Write-Output "Synced $src -> $dst"
