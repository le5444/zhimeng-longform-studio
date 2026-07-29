param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string[]]$SourcePaths,

    [string]$LibraryDir = ''
)

$ErrorActionPreference = 'Stop'
$script:Utf8NoBom = New-Object System.Text.UTF8Encoding -ArgumentList $false
$OutputEncoding = $script:Utf8NoBom
try { [Console]::OutputEncoding = $script:Utf8NoBom } catch { }
try { [Console]::InputEncoding = $script:Utf8NoBom } catch { }
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
if (Get-Variable -Name PSStyle -Scope Global -ErrorAction SilentlyContinue) {
    $PSStyle.OutputRendering = 'PlainText'
}
$ToolResultEnvelope = 'tomato_novel_auto_distill.v1'

function Resolve-DefaultLibraryDir {
    $pointer = Join-Path (Get-Location) 'distillation_library_path.txt'
    if (Test-Path -LiteralPath $pointer) {
        $value = (Get-Content -LiteralPath $pointer -Raw -Encoding UTF8).Trim()
        if ($value) {
            return $value
        }
    }
    $skillDir = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
    $skillsRoot = Split-Path -Parent $skillDir
    $packagedLibrary = Join-Path $skillsRoot 'novel-distillation\library'
    if (Test-Path -LiteralPath $packagedLibrary) {
        return $packagedLibrary
    }
    return (Join-Path (Get-Location) 'novel-distillation-library')
}

if ([string]::IsNullOrWhiteSpace($LibraryDir)) {
    $LibraryDir = Resolve-DefaultLibraryDir
}

$SkillDir = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$Extractor = Join-Path $SkillDir 'scripts\extract_epub_text.py'
$RawRoot = Join-Path $LibraryDir 'raw\tomato-downloads'

function New-Slug {
    param([string]$Value)
    $slug = $Value -replace '[\\/:*?"<>|]', '_' -replace '\s+', '_'
    if ($slug.Length -gt 48) {
        $slug = $slug.Substring(0, 48)
    }
    return $slug.Trim('_')
}

function Set-JsonProperty {
    param([object]$Object, [string]$Name, [object]$Value)
    if ($Object.PSObject.Properties[$Name]) {
        $Object.$Name = $Value
    } else {
        $Object | Add-Member -NotePropertyName $Name -NotePropertyValue $Value
    }
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Content
    )
    $dir = Split-Path -Parent $Path
    if ($dir) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $utf8NoBom)
}

function Get-ArtifactDigestInfo {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path) -or -not (Test-Path -LiteralPath $Path)) {
        return $null
    }
    $item = Get-Item -LiteralPath $Path
    if ($item.PSIsContainer) {
        return [pscustomobject]@{
            artifact_type = 'directory'
            size_bytes = $null
            sha256 = $null
        }
    }
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $hashBytes = $sha.ComputeHash($stream)
    } finally {
        $stream.Dispose()
        $sha.Dispose()
    }
    $hash = ([System.BitConverter]::ToString($hashBytes)).Replace('-', '').ToLowerInvariant()
    return [pscustomobject]@{
        artifact_type = 'file'
        size_bytes = [int64]$item.Length
        sha256 = $hash
    }
}

function New-ArtifactRecord {
    param([string]$Kind, [string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) {
        return $null
    }
    $info = Get-ArtifactDigestInfo -Path $Path
    if ($null -eq $info) {
        return $null
    }
    return [pscustomobject]@{
        kind = $Kind
        path = $Path
        artifact_type = $info.artifact_type
        size_bytes = $info.size_bytes
        sha256 = $info.sha256
    }
}


function Test-ValidSha256 {
    param([string]$Value)
    return -not [string]::IsNullOrWhiteSpace($Value) -and $Value -match '^[a-f0-9]{64}$'
}

function Assert-PreparedSourcePacket {
    param(
        [Parameter(Mandatory = $true)][string]$PacketDir,
        [Parameter(Mandatory = $true)][string]$SourcePath,
        [Parameter(Mandatory = $true)][string]$PacketSourcePath,
        [Parameter(Mandatory = $true)][string]$TextPath,
        [Parameter(Mandatory = $true)][string]$MetadataPath,
        [Parameter(Mandatory = $true)][string]$PacketManifestPath,
        [Parameter(Mandatory = $true)][string]$PacketSourceHashField,
        [Parameter(Mandatory = $true)][string]$PacketSourceArtifactKind,
        [Parameter(Mandatory = $true)][string]$ExternalSourceArtifactKind
    )

    $requiredPaths = @($PacketDir, $SourcePath, $PacketSourcePath, $TextPath, $MetadataPath, $PacketManifestPath)
    foreach ($path in $requiredPaths) {
        if (-not (Test-Path -LiteralPath $path)) {
            throw "Required source-packet artifact is missing: $path"
        }
    }
    try {
        $strictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
        $textContent = [System.IO.File]::ReadAllText($TextPath, $strictUtf8)
    } catch {
        throw "Prepared source-packet text is not valid UTF-8: $TextPath"
    }
    if ([string]::IsNullOrWhiteSpace($textContent)) {
        throw "Prepared source-packet text is empty: $TextPath"
    }

    $metadata = Get-Content -LiteralPath $MetadataPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $hashChecks = @(
        @{ field = 'source_file_sha256'; path = $SourcePath },
        @{ field = $PacketSourceHashField; path = $PacketSourcePath },
        @{ field = 'text_sha256'; path = $TextPath }
    )
    foreach ($check in $hashChecks) {
        $actual = Get-ArtifactDigestInfo -Path $check.path
        $declared = if ($metadata.PSObject.Properties[$check.field]) { "$($metadata.($check.field))" } else { '' }
        if (-not (Test-ValidSha256 -Value $declared)) {
            throw "Missing or invalid $($check.field) in packet metadata: $MetadataPath"
        }
        if ($declared -ne $actual.sha256) {
            throw "Hash mismatch for $($check.field): $($check.path)"
        }
    }

    $packetManifest = Get-Content -LiteralPath $PacketManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $artifactMap = @{}
    foreach ($artifact in @($packetManifest.artifacts)) {
        if ($artifact -and $artifact.kind) {
            $artifactMap["$($artifact.kind)"] = $artifact
        }
    }
    foreach ($requirement in @(
        @{ kind = $PacketSourceArtifactKind; path = $PacketSourcePath },
        @{ kind = 'packet_text'; path = $TextPath },
        @{ kind = 'packet_metadata'; path = $MetadataPath },
        @{ kind = $ExternalSourceArtifactKind; path = $SourcePath }
    )) {
        $artifact = $artifactMap[$requirement.kind]
        $actual = Get-ArtifactDigestInfo -Path $requirement.path
        if ($null -eq $artifact -or -not (Test-ValidSha256 -Value "$($artifact.sha256)") -or $artifact.sha256 -ne $actual.sha256) {
            throw "Packet manifest hash evidence is invalid for $($requirement.kind): $PacketManifestPath"
        }
    }

    return [pscustomobject]@{
        validation_status = 'verified'
        required_artifacts = @($PacketSourceArtifactKind, 'packet_text', 'packet_metadata', $ExternalSourceArtifactKind)
        hash_checks = @('source_file_sha256', $PacketSourceHashField, 'text_sha256')
        text_size_bytes = (Get-Item -LiteralPath $TextPath).Length
    }
}

function Write-PacketManifest {
    param(
        [Parameter(Mandatory = $true)][string]$PacketDir,
        [Parameter(Mandatory = $true)][string]$PacketLabel,
        [Parameter(Mandatory = $true)][string]$SourceRef,
        [Parameter(Mandatory = $true)][string]$SourceBackend,
        [Parameter(Mandatory = $true)][string]$DownloadScope,
        [Parameter(Mandatory = $true)][object[]]$Artifacts
    )
    $manifestPath = Join-Path $PacketDir 'packet.manifest.json'
    $payload = [ordered]@{
        packet_label = $PacketLabel
        source_ref = $SourceRef
        source_backend = $SourceBackend
        download_scope = $DownloadScope
        generated_at = (Get-Date).ToString('o')
        artifacts = @($Artifacts | Where-Object { $null -ne $_ })
    }
    Write-Utf8NoBom -Path $manifestPath -Content ($payload | ConvertTo-Json -Depth 20)
    return $manifestPath
}

function New-FileHandoffManifest {
    param(
        [Parameter(Mandatory = $true)][string]$Producer,
        [Parameter(Mandatory = $true)][string[]]$Consumers,
        [Parameter(Mandatory = $true)][object[]]$InputArtifacts,
        [Parameter(Mandatory = $true)][object[]]$OutputArtifacts,
        [string]$ValidationStatus = 'verified'
    )
    $safeInputs = @($InputArtifacts | Where-Object { $null -ne $_ })
    $safeOutputs = @($OutputArtifacts | Where-Object { $null -ne $_ })
    return [pscustomobject]@{
        schema_version = 'file_handoff_manifest.v1'
        producer = $Producer
        consumer = @($Consumers | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        validation_status = $ValidationStatus
        failure_codes = @()
        redactions = @('api_key', 'token', 'cookie', 'password')
        input_files = $safeInputs
        output_files = $safeOutputs
        sha256 = [ordered]@{
            input = @(
                $safeInputs |
                    Where-Object { $_.artifact_type -eq 'file' -and $_.sha256 } |
                    ForEach-Object {
                        [pscustomobject]@{
                            kind = $_.kind
                            path = $_.path
                            sha256 = $_.sha256
                        }
                    }
            )
            output = @(
                $safeOutputs |
                    Where-Object { $_.artifact_type -eq 'file' -and $_.sha256 } |
                    ForEach-Object {
                        [pscustomobject]@{
                            kind = $_.kind
                            path = $_.path
                            sha256 = $_.sha256
                        }
                    }
            )
        }
    }
}

function Resolve-FailureCode {
    param([string]$Message)
    if ($Message -like 'Unsupported source format:*') {
        return 'import_format_unsupported'
    }
    if ($Message -like 'Source path must be a file:*') {
        return 'import_path_not_file'
    }
    if ($Message -like 'Missing EPUB extractor:*') {
        return 'extractor_missing'
    }
    if ($Message -like '*Cannot find path*') {
        return 'source_path_missing'
    }
    if ($Message -like 'No valid source files were supplied*') {
        return 'source_paths_empty'
    }
    return 'source_packet_prepare_failed'
}

function Rollback-NewPacketDirs {
    param(
        [AllowEmptyCollection()][string[]]$PacketDirs = @(),
        [Parameter(Mandatory = $true)][string]$ExpectedRoot
    )
    $removed = @()
    $cleanupFailures = @()
    $rootFull = [System.IO.Path]::GetFullPath($ExpectedRoot).TrimEnd('\', '/')
    foreach ($packetDir in @($PacketDirs | Select-Object -Unique)) {
        if ([string]::IsNullOrWhiteSpace("$packetDir")) {
            continue
        }
        $packetFull = [System.IO.Path]::GetFullPath("$packetDir")
        $insideRoot = $packetFull.StartsWith(
            $rootFull + [System.IO.Path]::DirectorySeparatorChar,
            [System.StringComparison]::OrdinalIgnoreCase
        )
        if (-not $insideRoot) {
            $cleanupFailures += "refused_outside_raw_root:$packetFull"
            continue
        }
        if (-not (Test-Path -LiteralPath $packetFull)) {
            continue
        }
        try {
            Remove-Item -LiteralPath $packetFull -Recurse -Force -ErrorAction Stop
            if (Test-Path -LiteralPath $packetFull) {
                $cleanupFailures += "still_exists:$packetFull"
            } else {
                $removed += $packetFull
            }
        } catch {
            $cleanupFailures += "$packetFull :: $($_.Exception.Message)"
        }
    }
    return [pscustomobject]@{
        removed_packet_dirs = $removed
        cleanup_failures = $cleanupFailures
        orphan_risk = ($cleanupFailures.Count -gt 0)
    }
}

$script:InProgressPacketDir = $null
$script:CreatedPacketDirs = @()
$results = @()

try {
    if (-not (Test-Path -LiteralPath $Extractor)) {
        throw "Missing EPUB extractor: $Extractor"
    }

    New-Item -ItemType Directory -Force -Path $RawRoot | Out-Null

    foreach ($sourcePath in $SourcePaths) {
        if ([string]::IsNullOrWhiteSpace("$sourcePath")) {
            continue
        }

        $resolved = (Resolve-Path -LiteralPath $sourcePath).Path
        $item = Get-Item -LiteralPath $resolved
        if ($item.PSIsContainer) {
            throw "Source path must be a file: $resolved"
        }

        $ext = $item.Extension.ToLowerInvariant()
        if ($ext -notin @('.epub', '.txt')) {
            throw "Unsupported source format: $resolved. Expected .epub or .txt"
        }

        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $packetDir = Join-Path $RawRoot ("$stamp-" + (New-Slug $item.BaseName) + "-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
        New-Item -ItemType Directory -Force -Path $packetDir | Out-Null
        $script:InProgressPacketDir = $packetDir
        $script:CreatedPacketDirs += $packetDir

        $packetSource = Join-Path $packetDir $item.Name
        Copy-Item -LiteralPath $item.FullName -Destination $packetSource -Force

        if ($ext -eq '.epub') {
            $textPath = Join-Path $packetDir ($item.BaseName + '.txt')
            $metadataPath = Join-Path $packetDir ($item.BaseName + '.metadata.json')
            python $Extractor --epub $packetSource --out $textPath --meta $metadataPath | Out-Null
            $meta = Get-Content -LiteralPath $metadataPath -Raw -Encoding UTF8 | ConvertFrom-Json
        } else {
            $textPath = $packetSource
            $metadataPath = Join-Path $packetDir ($item.BaseName + '.metadata.json')
            $text = Get-Content -LiteralPath $packetSource -Raw -Encoding UTF8
            # Keep the pattern ASCII so Windows PowerShell 5.1 does not corrupt it
            # when this UTF-8 snapshot is loaded on a non-UTF-8 system code page.
            $chapterMarkers = [regex]::Matches($text, '(?m)^(\u7b2c[\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u767e\u5343\u4e07\u96f6\u3007\d]+[\u7ae0\u56de\u8282][^\r\n]{0,60})')
            $meta = [pscustomobject]@{
                title = $item.BaseName
                author = 'unknown'
                source_path = $item.FullName
                text_path = $textPath
                spine_item_count = 1
                chapter_marker_count = $chapterMarkers.Count
                chapter_markers_sample = @($chapterMarkers | Select-Object -First 80 | ForEach-Object { $_.Groups[1].Value })
                char_count = $text.Length
            }
        }

        $resolvedScope = 'full'
        if ($ext -eq '.epub' -and `
            $meta.PSObject.Properties['text_quality'] -and `
            $meta.text_quality -and `
            $meta.text_quality.PSObject.Properties['status'] -and `
            $meta.text_quality.status -eq 'mojibake_suspected') {
            Write-Verbose ("EPUB extracted text looks mojibaked: " + $item.FullName)
            $resolvedScope = 'full_but_text_degraded'
        }

        foreach ($pair in @(
            @{ Name = 'download_scope'; Value = $resolvedScope },
            @{ Name = 'range_start'; Value = $null },
            @{ Name = 'range_end'; Value = $null },
            @{ Name = 'downloaded_epub_path'; Value = $(if ($ext -eq '.epub') { $item.FullName } else { $null }) },
            @{ Name = 'query'; Value = $item.FullName },
            @{ Name = 'source_backend'; Value = 'generic_local_file' },
            @{ Name = 'sidecar_assets_removed'; Value = @() }
        )) {
            Set-JsonProperty -Object $meta -Name $pair.Name -Value $pair.Value
        }

        $sourceDigest = Get-ArtifactDigestInfo -Path $item.FullName
        $packetSourceDigest = Get-ArtifactDigestInfo -Path $packetSource
        $textDigest = Get-ArtifactDigestInfo -Path $textPath
        if ($sourceDigest) {
            Set-JsonProperty -Object $meta -Name 'source_file_sha256' -Value $sourceDigest.sha256
        }
        if ($packetSourceDigest) {
            Set-JsonProperty -Object $meta -Name 'packet_source_sha256' -Value $packetSourceDigest.sha256
        }
        if ($textDigest) {
            Set-JsonProperty -Object $meta -Name 'text_sha256' -Value $textDigest.sha256
        }
        if ($ext -eq '.epub' -and $sourceDigest) {
            Set-JsonProperty -Object $meta -Name 'downloaded_epub_sha256' -Value $sourceDigest.sha256
            Set-JsonProperty -Object $meta -Name 'packet_epub_sha256' -Value $packetSourceDigest.sha256
        }

        Write-Utf8NoBom -Path $metadataPath -Content ($meta | ConvertTo-Json -Depth 20)
        $packetManifestPath = Write-PacketManifest `
            -PacketDir $packetDir `
            -PacketLabel $item.BaseName `
            -SourceRef $item.FullName `
            -SourceBackend 'generic_local_file' `
            -DownloadScope $resolvedScope `
            -Artifacts @(
                (New-ArtifactRecord -Kind 'packet_source' -Path $packetSource),
                (New-ArtifactRecord -Kind 'packet_text' -Path $textPath),
                (New-ArtifactRecord -Kind 'packet_metadata' -Path $metadataPath),
                (New-ArtifactRecord -Kind 'source_file' -Path $item.FullName)
            )

        $packetValidation = Assert-PreparedSourcePacket `
            -PacketDir $packetDir `
            -SourcePath $item.FullName `
            -PacketSourcePath $packetSource `
            -TextPath $textPath `
            -MetadataPath $metadataPath `
            -PacketManifestPath $packetManifestPath `
            -PacketSourceHashField 'packet_source_sha256' `
            -PacketSourceArtifactKind 'packet_source' `
            -ExternalSourceArtifactKind 'source_file'

        $inputArtifacts = @(
            (New-ArtifactRecord -Kind 'source_file' -Path $item.FullName)
        )
        $outputArtifactsForItem = @(
            (New-ArtifactRecord -Kind 'source_packet_dir' -Path $packetDir),
            (New-ArtifactRecord -Kind 'packet_source' -Path $packetSource),
            (New-ArtifactRecord -Kind 'packet_text' -Path $textPath),
            (New-ArtifactRecord -Kind 'packet_metadata' -Path $metadataPath),
            (New-ArtifactRecord -Kind 'packet_manifest' -Path $packetManifestPath)
        )
        $fileHandoffManifest = New-FileHandoffManifest `
            -Producer 'generic_local_file' `
            -Consumers @('novel-distillation', 'novel-kb-manager') `
            -InputArtifacts $inputArtifacts `
            -OutputArtifacts $outputArtifactsForItem `
            -ValidationStatus $packetValidation.validation_status

        $results += [pscustomobject]@{
            query = $item.FullName
            title = $meta.title
            author = $meta.author
            epub_path = $(if ($ext -eq '.epub') { $packetSource } else { $null })
            downloaded_epub_path = $(if ($ext -eq '.epub') { $item.FullName } else { $null })
            text_path = $textPath
            metadata_path = $metadataPath
            packet_manifest_path = $packetManifestPath
            packet_dir = $packetDir
            char_count = $meta.char_count
            chapter_marker_count = $meta.chapter_marker_count
            download_scope = $resolvedScope
            range_start = $null
            range_end = $null
            download_dir = $item.DirectoryName
            library_dir = $LibraryDir
            sidecar_assets_removed = 0
            source_backend = 'generic_local_file'
            source_format = $ext.TrimStart('.')
            source_file_sha256 = $meta.source_file_sha256
            packet_source_sha256 = $meta.packet_source_sha256
            text_sha256 = $meta.text_sha256
            downloaded_epub_sha256 = $(if ($ext -eq '.epub') { $meta.downloaded_epub_sha256 } else { $null })
            packet_epub_sha256 = $(if ($ext -eq '.epub') { $meta.packet_epub_sha256 } else { $null })
            file_handoff_manifest = $fileHandoffManifest
            packet_validation = $packetValidation
        }
        $script:InProgressPacketDir = $null
    }

    if ($results.Count -lt 1) {
        throw 'No valid source files were supplied for source-packet preparation.'
    }

    $outputArtifacts = @()
    foreach ($result in $results) {
        foreach ($artifact in @(
            (New-ArtifactRecord -Kind 'source_packet_dir' -Path $result.packet_dir),
            (New-ArtifactRecord -Kind 'packet_metadata' -Path $result.metadata_path),
            (New-ArtifactRecord -Kind 'packet_text' -Path $result.text_path),
            (New-ArtifactRecord -Kind 'packet_epub' -Path $result.epub_path),
            (New-ArtifactRecord -Kind 'packet_manifest' -Path $result.packet_manifest_path),
            (New-ArtifactRecord -Kind 'imported_source_file' -Path $result.downloaded_epub_path),
            (New-ArtifactRecord -Kind 'imported_source_file' -Path $result.query)
        )) {
            if ($null -ne $artifact) {
                $outputArtifacts += $artifact
            }
        }
    }

    $warnings = @()
    foreach ($result in $results) {
        if ($result.download_scope -eq 'full_but_text_degraded') {
            $warnings += "text_quality_degraded:$($result.query)"
        }
    }
    $fullReadyCount = @($results | Where-Object { $_.download_scope -eq 'full' }).Count
    $readiness = ($fullReadyCount -eq $results.Count)

    [pscustomobject]@{
        tool_result_envelope = $ToolResultEnvelope
        ok = $true
        status = $(if ($readiness) { 'success' } else { 'success_with_gaps' })
        readiness = $readiness
        count = $results.Count
        items = $results
        committed_items = $results.Count
        committed_item_details = @($results | ForEach-Object {
            [pscustomobject]@{ query = $_.query; packet_dir = $_.packet_dir; download_scope = $_.download_scope }
        })
        orphan_risk = $false
        transaction = [ordered]@{
            mode = 'all_or_nothing_new_packets'
            created_packet_dirs = @($script:CreatedPacketDirs)
            removed_packet_dirs = @()
            cleanup_failures = @()
        }
        file_handoff_manifest = $(if ($results.Count -eq 1) { $results[0].file_handoff_manifest } else { @($results | ForEach-Object { $_.file_handoff_manifest }) })
        output_artifacts = $outputArtifacts
        warnings = $warnings
        failure_codes = @()
        success_criteria = [ordered]@{
            required_status = 'success'
            required_output_artifacts = @('source_packet_dir', 'packet_metadata', 'packet_text', 'packet_manifest')
            required_checkpoints = @('source_files_resolved', 'source_packets_prepared', 'file_handoff_manifest_verified')
            ready_for_distill_source_packet = $readiness
            ready_for_nlcl_sync = $false
        }
        checkpoints = @(
            [pscustomobject]@{
                name = 'source_files_resolved'
                status = 'passed'
                evidence = "count=$($results.Count)"
            },
            [pscustomobject]@{
                name = 'source_packets_prepared'
                status = 'passed'
                evidence = "$RawRoot (hash_manifest=true)"
            },
            [pscustomobject]@{
                name = 'file_handoff_manifest_verified'
                status = 'passed'
                evidence = "items=$($results.Count); validation_status=verified"
            }
        )
        fallback_manual_step = 'Inspect the source file path/format/encoding, repair the packet text or metadata if needed, then rerun prepare_downloaded_sources.ps1 before distillation.'
        next_action = $(if ($readiness) {
            'Pass metadata_path/text_path into novel-distillation, then run validate/health and sync_nlcl_after_distill.ps1.'
        } else {
            'Repair degraded/partial text before claiming full readiness; use --allow-incomplete only in the rank distill command when incomplete evidence was explicitly accepted.'
        })
        allow_incomplete_hint = 'Rank distillation requires full validated packets by default; --allow-incomplete is explicit opt-in only.'
    } | ConvertTo-Json -Depth 8
}
catch {
    $rollback = Rollback-NewPacketDirs -PacketDirs @($script:CreatedPacketDirs) -ExpectedRoot $RawRoot
    $remainingPacketDirs = @($script:CreatedPacketDirs | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -Unique)
    $message = $_.Exception.Message
    [pscustomobject]@{
        tool_result_envelope = $ToolResultEnvelope
        ok = $false
        status = 'failed'
        readiness = $false
        count = $remainingPacketDirs.Count
        items = @()
        committed_items = $remainingPacketDirs.Count
        committed_item_details = @($remainingPacketDirs | ForEach-Object { [pscustomobject]@{ packet_dir = $_ } })
        orphan_risk = [bool]$rollback.orphan_risk
        transaction = [ordered]@{
            mode = 'all_or_nothing_new_packets'
            created_packet_dirs = @($script:CreatedPacketDirs)
            removed_packet_dirs = @($rollback.removed_packet_dirs)
            cleanup_failures = @($rollback.cleanup_failures)
        }
        output_artifacts = @()
        warnings = @($rollback.cleanup_failures)
        failure_codes = @((Resolve-FailureCode -Message $message))
        success_criteria = [ordered]@{
            required_status = 'success'
            required_output_artifacts = @('source_packet_dir', 'packet_metadata', 'packet_text', 'packet_manifest')
            required_checkpoints = @('source_files_resolved', 'source_packets_prepared', 'file_handoff_manifest_verified')
            ready_for_distill_source_packet = $false
            ready_for_nlcl_sync = $false
        }
        checkpoints = @(
            [pscustomobject]@{
                name = 'source_packet_prepare'
                status = 'failed'
                evidence = $message
            }
        )
        fallback_manual_step = 'Fix the source path/format or extractor issue, or repair the source file manually before rerunning prepare_downloaded_sources.ps1.'
        next_action = 'Fix the source path/format or extractor issue, then rerun prepare_downloaded_sources.ps1.'
        error_message = $message
    } | ConvertTo-Json -Depth 8
    exit 1
}
