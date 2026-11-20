Code Studio for Windows
=======================

Install
  1. Make sure you extracted this zip (right-click > Extract All...).
  2. Double-click "Install Code Studio.cmd".
     If Windows says "Windows protected your PC", click More info > Run anyway
     (the installer is not code-signed).
  3. Answer the prompts. Python 3.12 is installed with winget if it is missing.
  4. Start Code Studio from the "Code Studio" shortcut (Desktop or Start Menu).
     Keep its console window open while you work; closing it stops Code Studio.

Where things go (%LOCALAPPDATA%\Programs\CodeStudio)
  app\          program files            replaced on every update
  venv\         Python environment       kept, packages synced on update
  data\         database and settings    never replaced (backed up before updates)
  config.json   ports, update server     kept
  logs\         install and update logs

Updates
  Code Studio checks the server it came from each time it starts and offers
  the update. You can also run "Update Code Studio" from the Start Menu, or
  install a newer package from the server's "Run Locally" page. Only the
  program files change; your sign-in, projects and settings are kept.

Remove
  Start Menu > Code Studio > Uninstall Code Studio.

This package contains a download token for your server account, used only to
fetch updates. Don't share it.
