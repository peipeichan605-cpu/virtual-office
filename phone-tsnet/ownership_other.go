//go:build !darwin && !linux && !freebsd && !openbsd && !netbsd

package main

import "os"

// This optional frontend currently requires Unix ownership checks. Other
// platforms must implement equivalent private ACL validation before use.
func ownedByCurrentUser(os.FileInfo) bool { return false }
