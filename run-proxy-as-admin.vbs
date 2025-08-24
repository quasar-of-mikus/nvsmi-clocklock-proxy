Set oShell = CreateObject("Shell.Application")
strCommand = "cmd.exe /c python ""C:\Applications\python-proxy-nvidia-smi\proxy.py""" 'required because nvidia-smi needs admin'
oShell.ShellExecute "cmd.exe", strCommand, "", "runas", 1