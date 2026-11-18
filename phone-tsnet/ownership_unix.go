//go:build darwin || linux || freebsd || openbsd || netbsd

package main

import (
	"os"
	"syscall"
)

func ownedByCurrentUser(fi os.FileInfo) bool {
	s, ok := fi.Sys().(*syscall.Stat_t)
	return ok && int(s.Uid) == os.Geteuid()
}
