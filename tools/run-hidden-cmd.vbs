' run-hidden-cmd.vbs - start a .cmd file with NO console window and return at once.
' Win32_Process.Create of "cmd.exe /c x.cmd" opens a visible console (Windows Terminal takes it over
' as a new window). wscript.exe is a GUI-subsystem program, and WshShell.Run with window style 0
' starts the console hidden, so the runner, viewer and capture never pop up while you work.
'
' Usage: wscript.exe //B //Nologo run-hidden-cmd.vbs <file.cmd>
Dim sh
Set sh = CreateObject("WScript.Shell")
sh.Run "cmd.exe /c """ & WScript.Arguments(0) & """", 0, False
