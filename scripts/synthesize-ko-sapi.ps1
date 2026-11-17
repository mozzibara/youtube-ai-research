param([Parameter(Mandatory=$true)][string]$OutputWav)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Speech
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
$text = [Console]::In.ReadToEnd()
if ([string]::IsNullOrWhiteSpace($text) -or $text.Length -gt 500) { throw "INVALID_TTS_INPUT" }
$voice = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $korean = @($voice.GetInstalledVoices() | Where-Object {
        $_.Enabled -and $_.VoiceInfo.Culture.Name -eq "ko-KR"
    })
    if ($korean.Count -lt 1) { throw "KOREAN_SAPI_VOICE_NOT_INSTALLED" }
    $voice.SelectVoice($korean[0].VoiceInfo.Name)
    $voice.Rate = -1
    $fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
        16000,
        [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
        [System.Speech.AudioFormat.AudioChannel]::Mono
    )
    $voice.SetOutputToWaveFile($OutputWav, $fmt)
    $voice.Speak($text)
    $voice.SetOutputToNull()
} finally {
    $voice.Dispose()
}
