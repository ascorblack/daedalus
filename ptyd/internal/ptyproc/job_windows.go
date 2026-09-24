//go:build windows

package ptyproc

import (
	"unsafe"

	"golang.org/x/sys/windows"
)

// Job is a Windows job object set to end every process in it when its last handle closes. It is
// the platform's answer to a process group that cannot be escaped: a child and all it starts belong
// to the job (unless they ask to break away, which this job does not allow), so ending the job ends
// the tree, and a daemon that dies closes the handle and takes the tree with it.
type Job struct {
	h windows.Handle
}

// NewJob creates an empty job that kills its processes when it is closed.
func NewJob() (*Job, error) {
	h, err := windows.CreateJobObject(nil, nil)
	if err != nil {
		return nil, err
	}
	var info windows.JOBOBJECT_EXTENDED_LIMIT_INFORMATION
	info.BasicLimitInformation.LimitFlags = windows.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
	if _, err := windows.SetInformationJobObject(h, windows.JobObjectExtendedLimitInformation,
		uintptr(unsafe.Pointer(&info)), uint32(unsafe.Sizeof(info))); err != nil {
		windows.CloseHandle(h)
		return nil, err
	}
	return &Job{h: h}, nil
}

// Assign puts a process in the job. Its children join as they are created.
func (j *Job) Assign(process windows.Handle) error {
	return windows.AssignProcessToJobObject(j.h, process)
}

// AssignPid is Assign for a process known by its id, as os/exec reports one.
func (j *Job) AssignPid(pid int) error {
	h, err := windows.OpenProcess(windows.PROCESS_SET_QUOTA|windows.PROCESS_TERMINATE, false, uint32(pid))
	if err != nil {
		return err
	}
	defer windows.CloseHandle(h)
	return j.Assign(h)
}

// Terminate ends every process in the job with the exit code given.
func (j *Job) Terminate(code uint32) error {
	return windows.TerminateJobObject(j.h, code)
}

// Close releases the job, which ends whatever is still in it.
func (j *Job) Close() error {
	if j.h == 0 {
		return nil
	}
	err := windows.CloseHandle(j.h)
	j.h = 0
	return err
}
