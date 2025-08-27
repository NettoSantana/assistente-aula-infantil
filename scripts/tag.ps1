param(
  [string]$msg = "backup"
)
$VER = "v" + (Get-Date -Format "yyyy.MM.dd-HHmm")
git tag -a $VER -m "$msg $VER"
git push origin $VER
Write-Host "Created and pushed tag $VER"
