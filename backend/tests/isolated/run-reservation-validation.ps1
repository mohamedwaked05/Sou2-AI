# Runs native tests only on a newly created, network-isolated PostgreSQL instance.
param(
    [string]$PostgresImage = 'pgvector/pgvector:0.8.0-pg17',
    [string]$RunnerImage = 'sou2ai-local-backend:catalogue-20261009'
)
$ErrorActionPreference = 'Stop'
$backendPath = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$nonce = [guid]::NewGuid().ToString('N').Substring(0, 12)
$database = "sou2ai_disposable_$nonce"
$containerName = "sou2ai-reservation-audit-$nonce"
$runnerName = "$containerName-runner"
$containerId = $null
$testExit = 1

function Assert-DisposableIdentity([string]$identity) {
    $details = docker inspect --format '{{json .}}' $identity | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or $details.Id -ne $containerId -or
        $details.Name -ne "/$containerName" -or
        $details.Config.Labels.'sou2ai.test.nonce' -ne $nonce -or
        $details.Config.Labels.'sou2ai.test' -ne 'reservation-audit' -or
        $details.HostConfig.NetworkMode -ne 'none' -or $details.Mounts -or
        ($details.HostConfig.PortBindings | ConvertTo-Json -Compress) -ne '{}' -or
        $details.HostConfig.Tmpfs.'/var/lib/postgresql/data' -ne 'rw,size=256m') {
        throw 'Disposable PostgreSQL identity/isolation verification failed.'
    }
}

# Require local images; never pull, rebuild, or touch existing containers.
foreach ($image in @($PostgresImage, $RunnerImage)) {
    docker image inspect $image --format '{{.Id}}'
    if ($LASTEXITCODE -ne 0) { throw "Required local image unavailable: $image" }
}
$existing = @(docker ps -a --format '{{.Names}}')
if ($existing -contains $containerName -or $existing -contains $runnerName) {
    throw 'Disposable container name collision.'
}
try {
    $containerId = docker run -d --pull never --name $containerName --network none `
        --label sou2ai.test=reservation-audit --label "sou2ai.test.nonce=$nonce" `
        --memory 512m --cpus 1 --tmpfs /var/lib/postgresql/data:rw,size=256m `
        -e POSTGRES_USER=reservation_test_admin -e "POSTGRES_DB=$database" `
        -e POSTGRES_HOST_AUTH_METHOD=trust $PostgresImage
    if ($LASTEXITCODE -ne 0) { throw 'Disposable PostgreSQL creation failed.' }
    Assert-DisposableIdentity $containerId
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        docker exec $containerId pg_isready -U reservation_test_admin -d $database *> $null
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) { throw 'Disposable PostgreSQL did not become ready.' }
    $identity = docker exec $containerId psql -U reservation_test_admin -d $database -Atc `
        "SELECT current_database(),current_user,(SELECT count(*) FROM pg_tables WHERE schemaname='public'),(SELECT count(*) FROM pg_database WHERE datname IN ('sou2ai_dev','sou2ai_test'));"
    if ($LASTEXITCODE -ne 0 -or $identity -ne "$database|reservation_test_admin|0|0") {
        throw 'Fresh disposable database verification failed.'
    }
    Write-Output "Verified disposable database: $database; container: $containerId"
    docker run --rm --pull never --name $runnerName --network "container:$containerId" `
        --label sou2ai.test=reservation-audit --label "sou2ai.test.nonce=$nonce" `
        --workdir /validation `
        --mount "type=bind,source=$backendPath/app,target=/validation/app,readonly" `
        --mount "type=bind,source=$backendPath/alembic,target=/validation/alembic,readonly" `
        --mount "type=bind,source=$backendPath/tests,target=/validation/tests,readonly" `
        -e "SOU2AI_DISPOSABLE_RUN=$nonce" -e ENVIRONMENT=testing -e ADMISSION_MODE=open `
        -e OWNER_CHAT_PROVIDER=mock -e CONVERSATION_SUMMARY_PROVIDER=mock `
        -e CUSTOMER_CHAT_PROVIDER=mock -e TRUSTED_HOSTS=testserver,localhost,127.0.0.1 `
        -e "POSTGRESQL_DATABASE_URL=postgresql+psycopg://sou2ai_runtime_login@127.0.0.1:5432/$database" `
        -e PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 -e PYTHONDONTWRITEBYTECODE=1 `
        $RunnerImage python -B -m pytest -c /dev/null -p no:cacheprovider `
        --confcutdir=tests/isolated tests/isolated -q --tb=short
    $testExit = $LASTEXITCODE
}
finally {
    if ($containerId) {
        Assert-DisposableIdentity $containerId
        docker rm --force $containerId
        if ($LASTEXITCODE -ne 0) { throw 'Disposable cleanup failed.' }
        Write-Output 'Removed the verified disposable PostgreSQL container and tmpfs data.'
    }
}
exit $testExit
