package legacy

// Go is walked and indexed but has no grammar, so parse_status == "unsupported".
// The symbol Helper is invisible to the parser — claims about it must resolve to
// UNKNOWN ("not indexed"), never a false REFUTED.
func Helper() int {
	return 42
}
