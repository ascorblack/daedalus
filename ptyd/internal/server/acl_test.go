package server

import "testing"

func TestTheRunDirectoryBelongsToItsUserAlone(t *testing.T) {
	sid := "S-1-5-21-1004336348-1177238915-682003330-1001"
	if got := OwnerOnlySDDL(sid, true); got != "D:P(A;OICI;FA;;;"+sid+")" {
		t.Errorf("directory: %s", got)
	}
	if got := OwnerOnlySDDL(sid, false); got != "D:P(A;;FA;;;"+sid+")" {
		t.Errorf("file: %s", got)
	}
}
