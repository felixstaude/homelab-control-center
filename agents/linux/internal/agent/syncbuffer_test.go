package agent_test

import (
	"bytes"
	"sync"
)

// syncBuffer is an io.Writer safe for concurrent use.
//
// The agent logs from its own goroutine while the test inspects the output
// from the test goroutine. A plain bytes.Buffer is not safe for that, and
// `go test -race` correctly rejects it.
type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

// String returns a snapshot of everything written so far.
func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}
