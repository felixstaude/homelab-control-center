// Package logging configures the agent's structured logger.
//
// Built on log/slog from the standard library. Text output is the default for
// interactive use; JSON is available for journald and log shippers. Both
// encode the same attributes, so a log pipeline sees a stable schema.
package logging

import (
	"io"
	"log/slog"
	"strings"
)

// Format selects the output encoding.
type Format string

const (
	FormatText Format = "text"
	FormatJSON Format = "json"
)

// New builds a logger writing to w.
//
// Unknown level strings fall back to info rather than failing: by the time
// this is called, config validation has already rejected bad values, and a
// logger that refuses to exist would hide the very error it should report.
func New(w io.Writer, level string, format Format) *slog.Logger {
	options := &slog.HandlerOptions{
		Level: parseLevel(level),
		// Source location is useful when debugging but noisy in production,
		// so it is attached only at debug level.
		AddSource: parseLevel(level) == slog.LevelDebug,
	}

	var handler slog.Handler
	if format == FormatJSON {
		handler = slog.NewJSONHandler(w, options)
	} else {
		handler = slog.NewTextHandler(w, options)
	}

	return slog.New(handler)
}

// parseLevel maps a configuration string onto a slog level.
func parseLevel(level string) slog.Level {
	switch strings.ToLower(strings.TrimSpace(level)) {
	case "debug":
		return slog.LevelDebug
	case "warn", "warning":
		return slog.LevelWarn
	case "error":
		return slog.LevelError
	default:
		return slog.LevelInfo
	}
}
