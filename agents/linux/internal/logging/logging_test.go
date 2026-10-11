package logging_test

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/logging"
)

func TestJSONFormatProducesParseableLines(t *testing.T) {
	t.Parallel()

	var buf bytes.Buffer
	logger := logging.New(&buf, "info", logging.FormatJSON)

	logger.Info("container stopped", "container", "nginx", "exit_code", 137)

	var entry map[string]any
	if err := json.Unmarshal(buf.Bytes(), &entry); err != nil {
		t.Fatalf("JSON output must be parseable, got %q: %v", buf.String(), err)
	}

	if entry["msg"] != "container stopped" {
		t.Errorf("msg = %v, want \"container stopped\"", entry["msg"])
	}
	if entry["container"] != "nginx" {
		t.Errorf("container = %v, want \"nginx\"", entry["container"])
	}
	if entry["level"] != "INFO" {
		t.Errorf("level = %v, want \"INFO\"", entry["level"])
	}
}

func TestTextFormatIsHumanReadable(t *testing.T) {
	t.Parallel()

	var buf bytes.Buffer
	logger := logging.New(&buf, "info", logging.FormatText)

	logger.Info("agent starting", "hostname", "nas-01")

	output := buf.String()
	if !strings.Contains(output, "msg=\"agent starting\"") {
		t.Errorf("expected a text-encoded message, got: %s", output)
	}
	if !strings.Contains(output, "hostname=nas-01") {
		t.Errorf("expected the attribute in the output, got: %s", output)
	}
}

func TestLevelFiltering(t *testing.T) {
	t.Parallel()

	tests := []struct {
		configured string
		emitDebug  bool
		emitInfo   bool
		emitWarn   bool
	}{
		{"debug", true, true, true},
		{"info", false, true, true},
		{"warn", false, false, true},
		{"error", false, false, false},
	}

	for _, tc := range tests {
		t.Run(tc.configured, func(t *testing.T) {
			t.Parallel()

			var buf bytes.Buffer
			logger := logging.New(&buf, tc.configured, logging.FormatText)

			logger.Debug("debug-line")
			logger.Info("info-line")
			logger.Warn("warn-line")

			output := buf.String()
			if got := strings.Contains(output, "debug-line"); got != tc.emitDebug {
				t.Errorf("debug emitted = %t, want %t", got, tc.emitDebug)
			}
			if got := strings.Contains(output, "info-line"); got != tc.emitInfo {
				t.Errorf("info emitted = %t, want %t", got, tc.emitInfo)
			}
			if got := strings.Contains(output, "warn-line"); got != tc.emitWarn {
				t.Errorf("warn emitted = %t, want %t", got, tc.emitWarn)
			}
		})
	}
}

func TestUnknownLevelFallsBackToInfo(t *testing.T) {
	t.Parallel()

	var buf bytes.Buffer
	// Config validation rejects bad levels before this point; the logger must
	// still work rather than suppress the error that would report them.
	logger := logging.New(&buf, "not-a-level", logging.FormatText)

	logger.Info("still-logging")
	logger.Debug("should-be-filtered")

	output := buf.String()
	if !strings.Contains(output, "still-logging") {
		t.Errorf("expected info to be emitted, got: %s", output)
	}
	if strings.Contains(output, "should-be-filtered") {
		t.Errorf("expected debug to be filtered at the info fallback, got: %s", output)
	}
}

func TestWarningAcceptsBothSpellings(t *testing.T) {
	t.Parallel()

	for _, level := range []string{"warn", "warning"} {
		var buf bytes.Buffer
		logger := logging.New(&buf, level, logging.FormatText)

		logger.Info("info-line")
		logger.Warn("warn-line")

		if strings.Contains(buf.String(), "info-line") {
			t.Errorf("level %q should filter info", level)
		}
		if !strings.Contains(buf.String(), "warn-line") {
			t.Errorf("level %q should emit warnings", level)
		}
	}
}
