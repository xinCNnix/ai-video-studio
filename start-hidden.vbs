' ===============================================
'  AI Video Studio - silent launcher (no windows)
'  Backend : FastAPI  on http://127.0.0.1:8000
'  Frontend: Next.js  on http://127.0.0.1:3000
'  Logs    : data\logs\backend.log / frontend.log
'  Stop    : double-click stop.bat
'  NOTE: keep this file ASCII-only (wscript uses ANSI)
' ===============================================
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

root = fso.GetParentFolderName(WScript.ScriptFullName)

' make sure the log folder exists
logDir = root & "\data\logs"
If Not fso.FolderExists(logDir) Then fso.CreateFolder(logDir)

' backend (hidden window, output to log)
backendCmd = "cmd /c cd /d """ & root & "\backend"" && .venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 > ..\data\logs\backend.log 2>&1"
shell.Run backendCmd, 0, False

' frontend: prefer production build when present, fallback to dev mode
If fso.FileExists(root & "\frontend\.next\BUILD_ID") Then
    frontendCmd = "cmd /c cd /d """ & root & "\frontend"" && npm run start > ..\data\logs\frontend.log 2>&1"
Else
    frontendCmd = "cmd /c cd /d """ & root & "\frontend"" && npm run dev > ..\data\logs\frontend.log 2>&1"
End If
shell.Run frontendCmd, 0, False

' wait for servers, then open the app in browser
WScript.Sleep 8000
shell.Run "http://127.0.0.1:3000"
