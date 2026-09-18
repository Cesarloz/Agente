param([switch]$NoBrowser)

$ErrorActionPreference = 'Stop'
$projectDirectory = Split-Path -Parent $PSScriptRoot
$launcherExitCode = 0

function Get-LocalServiceUrl {
    param([string]$Service, [int]$ContainerPort)

    $publishedAddress = & $script:dockerPath compose port $Service $ContainerPort
    if ($LASTEXITCODE -ne 0) {
        throw "No se pudo consultar el puerto del servicio $Service."
    }
    foreach ($address in $publishedAddress) {
        if ($address -match ':(\d+)\s*$') {
            return "http://localhost:$($Matches[1])"
        }
    }
    throw "El servicio $Service no tiene un puerto local publicado."
}

function Test-LocalHealth {
    param([string]$Url)

    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
        return $response.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

Push-Location -LiteralPath $projectDirectory
try {
    Write-Host 'Iniciando Reflexia en este equipo...' -ForegroundColor Cyan
    $dockerCommand = Get-Command docker.exe -ErrorAction SilentlyContinue
    if ($dockerCommand) {
        $script:dockerPath = $dockerCommand.Source
    }
    else {
        $script:dockerPath = Join-Path $env:ProgramFiles 'Docker\Docker\resources\bin\docker.exe'
        if (-not (Test-Path -LiteralPath $script:dockerPath)) {
            throw 'No se encuentra Docker. Este proyecto requiere Docker Desktop con contenedores Linux.'
        }
    }

    try {
        $dockerOperatingSystem = & $script:dockerPath info --format '{{.OSType}}' 2>$null
        if ($LASTEXITCODE -ne 0) { throw 'Docker no responde.' }
    }
    catch {
        throw 'Abre Docker Desktop, espera a que el motor este iniciado y vuelve a ejecutar Iniciar-Reflexia.cmd.'
    }
    if ($dockerOperatingSystem -ne 'linux') {
        throw 'Configura Docker Desktop para usar contenedores Linux y vuelve a ejecutar el lanzador.'
    }

    & $script:dockerPath compose version
    if ($LASTEXITCODE -ne 0) {
        throw 'Docker Compose no esta disponible. Revisa tu instalacion de Docker Desktop.'
    }

    Write-Host 'Preparando servicios. El primer arranque puede tardar varios minutos.'
    & $script:dockerPath compose up -d --build
    if ($LASTEXITCODE -ne 0) {
        throw 'No se pudieron iniciar los servicios. Revisa el error de Docker mostrado arriba.'
    }

    $adminUrl = Get-LocalServiceUrl -Service 'admin' -ContainerPort 8501
    $apiUrl = Get-LocalServiceUrl -Service 'api' -ContainerPort 8000
    Write-Host 'Esperando a que la API y la interfaz respondan...'
    $deadline = (Get-Date).AddSeconds(180)
    $lastProgress = Get-Date
    $servicesReady = $false
    do {
        $apiReady = Test-LocalHealth -Url "$apiUrl/health"
        $adminReady = Test-LocalHealth -Url "$adminUrl/_stcore/health"
        if ($apiReady -and $adminReady) {
            $servicesReady = $true
            break
        }
        if (((Get-Date) - $lastProgress).TotalSeconds -ge 15) {
            Write-Host 'Los servicios siguen iniciando...'
            $lastProgress = Get-Date
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)

    if (-not $servicesReady) {
        throw "Los servicios no respondieron a tiempo. Consulta: docker compose logs --tail=100 api admin worker"
    }

    Write-Host "Reflexia esta lista: $adminUrl" -ForegroundColor Green
    Write-Host "Documentacion de la API: $apiUrl/docs"
    Write-Host 'Para detenerla conservando tus datos, ejecuta en esta carpeta: docker compose down'
    if (-not $NoBrowser) {
        try {
            Start-Process -FilePath $adminUrl
        }
        catch {
            Write-Host "No se pudo abrir el navegador automaticamente. Abre esta direccion: $adminUrl" -ForegroundColor Yellow
        }
    }
}
catch {
    Write-Host "No se completo el arranque: $($_.Exception.Message)" -ForegroundColor Red
    $launcherExitCode = 1
}
finally {
    Pop-Location
}

exit $launcherExitCode
