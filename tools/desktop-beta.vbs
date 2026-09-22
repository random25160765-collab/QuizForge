' QuizForge - Windows desktop launcher (beta / source-tree runtime)
'
' Double-click this file:
'   1. if the service is not running, start it **inside WSL** (build the web
'      bundle first, then let uvicorn detach), and wait for /api/health;
'   2. open the default browser.
' When the service is already up it just opens the browser - so a second
' click is instant.
'
' This is the SOURCE-TREE dev runtime (uvicorn inside WSL). It is NOT the
' packaged single-file build (make package / dist / dist-release): that one
' ships its own runtime, needs no WSL, and must not be started by this script.
'
' Log:   /tmp/quizforge-beta.log   (inside WSL)
' Ports: 8100
' Change DISTRO / REPO below on another machine.
'
' Notes are in English on purpose: wscript reads .vbs with the system ANSI
' code page, so non-ASCII comments risk mojibake.

Option Explicit

' ---- where is the repo? ----------------------------------------------------
' No path is hard-coded here. This file lives at <repo>/tools/desktop-beta.vbs,
' so when it is opened through the WSL share (\\wsl$\<distro>\...\desktop-beta.vbs)
' both the distro and the repo come from that path. Open it some other way and
' there is nothing to infer from, so set the environment variables QF_DISTRO
' (e.g. Ubuntu) and QF_REPO (a WSL path, e.g. /home/me/QuizForge).
' Resolved below, after `sh` exists; inferred values are the fallback.
' ---------------------------------------------------------------------------

Const URL = "http://127.0.0.1:8100/"
Const HEALTH = "http://127.0.0.1:8100/api/health"
Const WAIT_MAX = 60          ' seconds; first run needs make web + uvicorn boot

Dim sh
Set sh = CreateObject("WScript.Shell")

' ---- resolve DISTRO / REPO (see the note at the top) ------------------------
Dim DISTRO, REPO
Dim inferredDistro, inferredRepo
inferredDistro = ""
inferredRepo = ""
InferFromOwnPath inferredDistro, inferredRepo
DISTRO = EnvOr("QF_DISTRO", inferredDistro)
REPO = EnvOr("QF_REPO", inferredRepo)
If DISTRO = "" Or REPO = "" Then
  MsgBox "QuizForge: cannot tell where the repo is." & vbCrLf & vbCrLf & _
         "Open this file from inside the WSL share (\\wsl$\<distro>\...\tools\desktop-beta.vbs)," & vbCrLf & _
         "or set QF_DISTRO (e.g. Ubuntu) and QF_REPO (e.g. /home/me/QuizForge).", _
         48, "QuizForge"
  WScript.Quit 1
End If
' ---------------------------------------------------------------------------

' ---- is it already up? -----------------------------------------------------
Function IsUp()
  Dim http
  IsUp = False
  On Error Resume Next
  Set http = CreateObject("MSXML2.ServerXMLHTTP.6.0")
  http.SetTimeouts 1500, 1500, 1500, 1500
  http.open "GET", HEALTH, False
  http.send
  If Err.Number = 0 Then
    If http.status = 200 Then IsUp = True
  End If
  On Error GoTo 0
End Function

If Not IsUp() Then
  ' ---- start it in WSL: hidden window, do not wait (uvicorn detaches) -------
  ' All the work lives in tools/beta-serve.sh (idempotent: it does nothing when
  ' the service is already up, and it builds the web bundle first).
  Dim cmd
  cmd = "wsl.exe -d " & DISTRO & " -e bash -lc ""bash " & REPO & "/tools/beta-serve.sh"""
  sh.Run cmd, 0, False

  Dim waited
  waited = 0
  Do While waited < WAIT_MAX
    WScript.Sleep 1000
    waited = waited + 1
    If IsUp() Then Exit Do
  Loop

  If Not IsUp() Then
    MsgBox "QuizForge did not come up in " & WAIT_MAX & "s." & vbCrLf & vbCrLf & _
           "Check the log inside WSL:" & vbCrLf & "  /tmp/quizforge-beta.log", _
           48, "QuizForge"
    WScript.Quit 1
  End If
End If

' ---- open the browser ------------------------------------------------------
sh.Run URL, 1, False

' ---- helpers ----------------------------------------------------------------

' %QF_XXX% when it is set, otherwise the inferred value (which may be "").
' ExpandEnvironmentStrings leaves an unknown name untouched, which is how we
' tell "not set" from "set to empty".
Function EnvOr(name, inferred)
  Dim v
  v = sh.ExpandEnvironmentStrings("%" & name & "%")
  If v = "%" & name & "%" Then
    EnvOr = inferred
  Else
    EnvOr = v
  End If
End Function

' Pull the distro and the repo path out of the path this script was opened
' through, e.g.
'   \\wsl$\Ubuntu\home\me\QuizForge\tools\desktop-beta.vbs
'   -> distro "Ubuntu", repo "/home/me/QuizForge"
' Either name may also be \\wsl.localhost\... . Leaves both as "" when the file
' was opened some other way (copied out, local path), so the caller can fall
' back to the environment variables and then give up with a clear message.
Sub InferFromOwnPath(ByRef distro, ByRef repo)
  Dim p, rest, i
  distro = ""
  repo = ""
  p = WScript.ScriptFullName
  If LCase(Left(p, 7)) = "\\wsl$\" Then
    rest = Mid(p, 8)
  ElseIf LCase(Left(p, 16)) = "\\wsl.localhost\" Then
    rest = Mid(p, 17)
  Else
    Exit Sub
  End If
  i = InStr(rest, "\")            ' distro name
  If i = 0 Then Exit Sub
  distro = Left(rest, i - 1)
  rest = Mid(rest, i)             ' \home\me\QuizForge\tools\desktop-beta.vbs
  i = InStrRev(rest, "\")         ' strip the file name
  If i = 0 Then Exit Sub
  rest = Left(rest, i - 1)        ' \home\me\QuizForge\tools
  i = InStrRev(rest, "\")         ' strip \tools
  If i = 0 Then Exit Sub
  rest = Left(rest, i - 1)        ' \home\me\QuizForge
  repo = Replace(rest, "\", "/")
End Sub
