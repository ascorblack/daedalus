//go:build unix

package hooks

import "syscall"

// noFollow refuses to open a symlink in place of the file: an overlay file is always created new,
// and a link planted under its name must not redirect the write.
const noFollow = syscall.O_NOFOLLOW
