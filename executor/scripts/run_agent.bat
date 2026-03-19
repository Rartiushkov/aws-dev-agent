@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "GOAL=%*"
if "%GOAL%"=="" set "GOAL=list lambda"

set "OUTPUT_FILE=%TEMP%\aws_dev_agent_output.txt"

python -m cli.agent "%GOAL%" > "%OUTPUT_FILE%" 2>&1
type "%OUTPUT_FILE%"

echo.
echo ===PARSED_AGENT_RESULT===
powershell -NoProfile -Command ^
  "$content = Get-Content -Raw '%OUTPUT_FILE%';" ^
  "$match = [regex]::Match($content, '===AGENT_RESULT_START===\r?\n(?<body>.*?)\r?\n===AGENT_RESULT_END===', 'Singleline');" ^
  "if ($match.Success) { $match.Groups['body'].Value } else { 'status=failure'; 'details=Agent result block not found' }"

endlocal
