package main

import (
	"os"
	"os/exec"
)

// Windows through a toast, raised by the notification manager Windows itself provides. PowerShell
// is how a program without a WinRT binding reaches it, and PowerShell is on every Windows.
//
// The toast document (toastXML, in notify.go) travels in the environment rather than in the script:
// a title with a quote in it would otherwise end the string it is in, and the text comes from the
// agent. The document itself is escaped when it is built.
//
// The toast is raised under PowerShell's application identity. Windows requires an installed
// AppUserModelID to show one at all, and the launcher — a folder with an executable in it, and no
// installer — does not have one of its own. The name in the toast is therefore Windows PowerShell;
// the title line in the toast itself says what it is about. A click reaches the launcher through
// the daedalus:// scheme named in the document, not through open.
const toastScript = `
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$document = [Windows.Data.Xml.Dom.XmlDocument]::new()
$document.LoadXml($env:DAEDALUS_TOAST_XML)
$toast = [Windows.UI.Notifications.ToastNotification]::new($document)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe').Show($toast)
`

func notify(n Notification, _ func(string)) error {
	cmd := exec.Command("powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", toastScript)
	cmd.Env = append(os.Environ(), "DAEDALUS_TOAST_XML="+toastXML(n))
	if out, err := cmd.CombinedOutput(); err != nil {
		return combinedError("powershell", out, err)
	}
	return nil
}
