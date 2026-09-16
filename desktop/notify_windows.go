package main

import (
	"os"
	"os/exec"
)

// Windows through a toast, raised by the notification manager Windows itself provides. PowerShell
// is how a program without a WinRT binding reaches it, and PowerShell is on every Windows.
//
// The text travels in the environment rather than in the script: a title with a quote in it would
// otherwise end the string it is in, and the text comes from the agent's own inbox.
//
// The toast is raised under PowerShell's application identity. Windows requires an installed
// AppUserModelID to show one at all, and the launcher — a folder with an executable in it, and no
// installer — does not have one of its own. The name in the toast is therefore Windows PowerShell;
// the title line in the toast itself says Daedalus.
const toastScript = `
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$text = $template.GetElementsByTagName('text')
$text[0].AppendChild($template.CreateTextNode($env:DAEDALUS_TOAST_TITLE)) > $null
$text[1].AppendChild($template.CreateTextNode($env:DAEDALUS_TOAST_BODY)) > $null
$toast = [Windows.UI.Notifications.ToastNotification]::new($template)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe').Show($toast)
`

func notify(n Notification) error {
	cmd := exec.Command("powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", toastScript)
	cmd.Env = append(os.Environ(), "DAEDALUS_TOAST_TITLE="+n.Title, "DAEDALUS_TOAST_BODY="+n.Body)
	if out, err := cmd.CombinedOutput(); err != nil {
		return combinedError("powershell", out, err)
	}
	return nil
}
