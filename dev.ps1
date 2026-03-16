$port = 8000

Write-Host "Starting uvicorn on 127.0.0.1:$port..."

try {
    uvicorn app.main:app --host 127.0.0.1 --port $port --reload
} finally {
    Write-Host "Cleaning up port $port..."
    $pids = netstat -ano | Select-String ":$port " | ForEach-Object {
        ($_ -split '\s+')[-1]
    } | Sort-Object -Unique | Where-Object { $_ -match '^\d+$' -and $_ -ne '0' }

    foreach ($p in $pids) {
        taskkill /F /T /PID $p 2>$null
        Write-Host "Killed PID $p"
    }
    Write-Host "Done."
}
