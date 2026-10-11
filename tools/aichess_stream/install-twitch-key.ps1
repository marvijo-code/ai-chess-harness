# Puts the Twitch stream key that is on the clipboard onto the VPS (~/.config/ai-chess/twitch-stream.env,
# mode 600). The key goes over ssh stdin: it is never written on the laptop and never printed.
# Prints only its length and the first 8 characters of its SHA-256.
#   pwsh tools/aichess_stream/install-twitch-key.ps1            (copy the key in the Twitch dashboard first)
#   then on the VPS: sudo systemctl restart ai-chess-stream
param(
  [string]$Vps = "marvijo@145.239.75.15",
  [string]$RtmpUrl = "rtmp://ingest.global-contribute.live-video.net/app"
)
$ErrorActionPreference = "Stop"
$key = "$(Get-Clipboard -Raw)".Trim()
if ($key -notmatch '^live_[A-Za-z0-9_]{20,}$') {
  throw "The clipboard does not hold a Twitch stream key (it starts with live_). Nothing was sent."
}
$sha = [BitConverter]::ToString([Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($key))).Replace("-", "").ToLower()
$remote = "umask 077; mkdir -p ~/.config/ai-chess; k=`$(tr -d '\r\n'); { echo 'TWITCH_RTMP_URL=$RtmpUrl'; printf 'TWITCH_STREAM_KEY=%s\n' `"`$k`"; } > ~/.config/ai-chess/twitch-stream.env; chmod 600 ~/.config/ai-chess/twitch-stream.env; echo `"on the VPS: `${#k} characters`""
$key | ssh $Vps $remote
if ($LASTEXITCODE -ne 0) { throw "ssh failed with exit code $LASTEXITCODE" }
"Twitch key installed: length $($key.Length), sha256 $($sha.Substring(0, 8))"
