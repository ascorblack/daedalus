package server

// OwnerOnlySDDL is the security descriptor, in SDDL, that gives the user whose SID is sid full
// control and nobody else anything, with inheritance from the parent cut ("P"). With inherit, the
// entry also passes to the files and directories created inside ("OICI"), so a token written into
// the run directory is never readable by another account, not even for the moment between its
// creation and its own descriptor being set.
//
// On Windows this is what the 0700 and 0600 modes of the Unix run directory mean. It is text built
// without the system, so every platform's tests check it.
func OwnerOnlySDDL(sid string, inherit bool) string {
	flags := ""
	if inherit {
		flags = "OICI"
	}
	return "D:P(A;" + flags + ";FA;;;" + sid + ")"
}
