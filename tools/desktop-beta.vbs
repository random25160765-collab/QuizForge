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

' ---- change these two on another machine -----------------------------------
Const DISTRO = "Ubuntu"
Const REPO = "/home/rd/Desktop/quizforge"
' ---------------------------------------------------------------------------

Const URL = "http://127.0.0.1:8100/"
Const HEALTH = "http://127.0.0.1:8100/api/health"
Const WAIT_MAX = 60          ' seconds; first run needs make web + uvicorn boot

Dim sh
Set sh = CreateObject("WScript.Shell")

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
