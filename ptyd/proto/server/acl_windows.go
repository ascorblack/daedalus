//go:build windows

package server

import (
	"golang.org/x/sys/windows"
)

// RestrictDir gives a directory, and through inheritance what is created in it, to this process's
// user alone. Mode bits mean nothing to Windows; a directory under a shared folder would otherwise
// inherit an access list that lets every user of the machine read the token.
func RestrictDir(path string) error { return restrict(path, true) }

// restrictFile gives one file to this process's user alone.
func restrictFile(path string) error { return restrict(path, false) }

func restrict(path string, inherit bool) error {
	user, err := windows.GetCurrentProcessToken().GetTokenUser()
	if err != nil {
		return err
	}
	sd, err := windows.SecurityDescriptorFromString(OwnerOnlySDDL(user.User.Sid.String(), inherit))
	if err != nil {
		return err
	}
	dacl, _, err := sd.DACL()
	if err != nil {
		return err
	}
	return windows.SetNamedSecurityInfo(path, windows.SE_FILE_OBJECT,
		windows.DACL_SECURITY_INFORMATION|windows.PROTECTED_DACL_SECURITY_INFORMATION, nil, nil, dacl, nil)
}
