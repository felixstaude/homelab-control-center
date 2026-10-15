package collect

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

// MaxSensors bounds how many sensor series one host may report.
//
// Cardinality, as elsewhere. A 64-core Threadripper with per-core
// temperatures, eight NVMe drives and a motherboard full of fans gets close
// to this; a homelab machine is nowhere near it.
const MaxSensors = 128

// MaxSensorNameChars is the longest sensor identity that can be a label.
//
// The controller refuses a label value over 64 characters. A sensor whose
// identity is longer is **skipped**, not truncated, for the same reason a
// long mount point is: two sensors truncated to the same 64 characters would
// become one series, silently averaging two readings that have nothing to do
// with each other.
const MaxSensorNameChars = 64

// Plausible bounds on a temperature reading, in degrees Celsius.
//
// Sensors lie. A disconnected thermistor reads -273.2 °C, a chip that has
// not been initialised reads 0, and a bad I²C read produces values in the
// thousands. None of those is a temperature, and a chart with a -273 in it
// is a chart with a useless y-axis — which hides the real readings beside
// it. Outside this range the sample is dropped rather than clamped: clamping
// would turn a broken sensor into a plausible-looking one.
const (
	MinPlausibleCelsius = -40
	MaxPlausibleCelsius = 200
)

// MaxPlausibleRPM bounds a fan reading. Server fans reach 20,000 RPM; a
// tachometer reporting six digits is miscounting pulses.
const MaxPlausibleRPM = 60000

// Sensors reports temperatures and fan speeds from /sys/class/hwmon.
//
// # Only what the hardware exposes
//
// There is no inference here and no fallback. A host with no hwmon devices
// reports the module **unavailable, with the reason**, rather than producing
// a plausible number from somewhere else — which is the single rule this
// whole package is shaped around and matters most for sensors, where a wrong
// number looks exactly like a right one.
//
// # Why /sys/class/thermal is deliberately not read
//
// The thermal zone interface looks like extra coverage and is mostly a
// second view of the same silicon:
//
//   - On the machine this was written against, `thermal_zone0` and `hwmon5`
//     resolve to **the same device path**. Reading both would report one
//     sensor twice.
//   - `thermal_zone1` has type `x86_pkg_temp`, which is the same physical
//     sensor `coretemp` already reports as "Package id 0" — through a
//     *different* device, so no path comparison catches it.
//   - A zone's `type` is a vendor string — `acpitz`, `TCPU`, `INT3400` —
//     with no reliable mapping to a component. Labelling one "CPU" would be
//     the guess the product rule forbids.
//
// So hwmon only. The documented cost: a host that exposes thermal zones and
// no hwmon device reports no temperatures at all, and says so.
//
// # Sensor identity, and why it is not the hwmon number
//
// hwmon directories are numbered in **probe order**, which changes between
// boots. A series keyed on `hwmon3` would silently continue one chart with
// another chip's readings after a reboot — the worst kind of wrong, because
// the line stays smooth.
//
// So the identity is the chip's own `name` plus the sensor's own `label`:
// `coretemp Package id 0`, `nvme Composite`. That is stable and readable,
// and it is not quite enough: a machine with two NVMe drives has **two**
// chips named `nvme`, each reporting `Composite`, and merging them would
// average two drives' temperatures into one series. Found on the machine
// this was written on, which has exactly that.
//
// The disambiguator is the device basename behind the chip, appended when it
// differs from the chip name: `nvme-nvme0 Composite`,
// `nvme-nvme1 Composite`. Suppressed when the two normalise to the same
// string, because `r8169_0_600:00` and `r8169-0-600:00` are the same name
// written twice and `r8169_0_600:00-r8169-0-600:00` helps nobody.
type Sensors struct {
	// SysRoot is where /sys is mounted. A field so tests read a committed
	// fixture tree rather than the build machine's hardware, which differs
	// between every machine this could run on.
	SysRoot string
}

// NewSensors builds a sensor collector.
func NewSensors(sysRoot string) *Sensors {
	if sysRoot == "" {
		sysRoot = DefaultSysRoot
	}
	return &Sensors{SysRoot: sysRoot}
}

// Module implements [Collector].
func (s *Sensors) Module() string { return "sensors" }

// Available implements [Collector].
//
// Three distinguishable answers, because they call for different actions: no
// hwmon directory at all (a container without /sys, or a kernel without the
// subsystem), a directory with no readable sensors (a virtual machine, which
// is the common case), and working.
func (s *Sensors) Available() Availability {
	root := filepath.Join(s.SysRoot, "class", "hwmon")
	if _, err := os.Stat(root); err != nil {
		return Availability{
			Available: false,
			Reason: fmt.Sprintf(
				"%s does not exist, so this host exposes no hardware sensors", root,
			),
		}
	}
	readings, err := s.read()
	if err != nil {
		return Availability{
			Available: false,
			Reason:    fmt.Sprintf("%s cannot be read: %v", root, err),
		}
	}
	if len(readings) == 0 {
		return Availability{
			Available: false,
			Reason: "this host has no readable temperature or fan sensors. Virtual " +
				"machines usually have none, and nothing is reported rather than " +
				"a figure derived from somewhere else",
		}
	}
	return Availability{Available: true}
}

// Collect implements [Collector].
func (s *Sensors) Collect(_ context.Context) ([]Sample, error) {
	readings, err := s.read()
	if err != nil {
		return nil, err
	}
	samples := make([]Sample, 0, len(readings))
	for _, reading := range readings {
		samples = append(samples, Sample{
			Metric: reading.metric,
			Labels: map[string]string{"sensor": reading.sensor},
			Value:  reading.value,
		})
	}
	kept, _ := keepValid(samples)
	sortSamples(kept)
	return kept, nil
}

// reading is one sensor's current value.
type reading struct {
	metric string
	sensor string
	value  float64
}

// inputFile matches the sysfs files this reads: temp1_input, fan2_input.
//
// Only `_input`. The neighbouring `_crit`, `_max`, `_min` and `_alarm` files
// are thresholds rather than measurements: they are constants, so storing
// one per interval would be the same number a million times, and the data
// model has no place for a constant. An interface that wants to say
// "32 °C of 95 °C critical" needs a different mechanism, which does not
// exist yet.
var inputFile = regexp.MustCompile(`^(temp|fan)([0-9]+)_input$`)

// read walks every hwmon device and returns the readings it can trust.
func (s *Sensors) read() ([]reading, error) {
	root := filepath.Join(s.SysRoot, "class", "hwmon")
	entries, err := os.ReadDir(root)
	if err != nil {
		return nil, fmt.Errorf("cannot read %s: %w", root, err)
	}

	// Sorted, so the MaxSensors cut is deterministic rather than filesystem
	// order — otherwise a host at the limit would report a different subset
	// on each collection and produce a different series every time.
	names := make([]string, 0, len(entries))
	for _, entry := range entries {
		names = append(names, entry.Name())
	}
	sort.Strings(names)

	readings := make([]reading, 0, 16)
	for _, name := range names {
		device := filepath.Join(root, name)
		chip := s.chipName(device)
		if chip == "" {
			// A hwmon directory with no readable `name`. Skipped rather than
			// given a positional identity: `hwmon3` is exactly the unstable
			// key this avoids.
			continue
		}
		files, err := os.ReadDir(device)
		if err != nil {
			// Unreadable device. One broken chip must not blank out the rest
			// of the host's sensors.
			continue
		}
		for _, file := range files {
			match := inputFile.FindStringSubmatch(file.Name())
			if match == nil {
				continue
			}
			if len(readings) >= MaxSensors {
				break
			}
			found, ok := s.readOne(device, chip, match[1], match[2])
			if ok {
				readings = append(readings, found)
			}
		}
	}
	return readings, nil
}

// readOne turns one `tempN_input` or `fanN_input` into a reading.
func (s *Sensors) readOne(device, chip, kind, index string) (reading, bool) {
	raw, err := readSysfsValue(filepath.Join(device, kind+index+"_input"))
	if err != nil {
		// A sensor that errors on read is a real and common state — a chip
		// that is asleep, a drive that has spun down, an I²C bus that is
		// busy. Nothing is reported for it this interval.
		return reading{}, false
	}

	// The sensor's own label, falling back to its positional name *within
	// the chip*. `temp1` is stable for a given chip in a way `hwmon3` is
	// not: the index is assigned by the driver from the hardware's own
	// channel numbering, not by probe order.
	label := readSysfsString(filepath.Join(device, kind+index+"_label"))
	if label == "" {
		label = kind + index
	}

	identity := chip + " " + label
	if len(identity) > MaxSensorNameChars {
		return reading{}, false
	}

	switch kind {
	case "temp":
		// Millidegrees Celsius, which is what every hwmon temperature file
		// reports. Dividing by 1000 is the whole conversion.
		celsius := raw / 1000
		if celsius < MinPlausibleCelsius || celsius > MaxPlausibleCelsius {
			// A disconnected thermistor reads -273.2; an uninitialised chip
			// reads 0 in millidegrees, which is 0 °C and *is* plausible, so
			// that one is reported. Dropped rather than clamped: clamping
			// turns a broken sensor into a believable one.
			return reading{}, false
		}
		return reading{metric: "temperature", sensor: identity, value: celsius}, true
	case "fan":
		// RPM directly. A stopped fan reads 0 and that is a measurement, not
		// an absence — a 0 here is how "the fan is not spinning" looks.
		if raw < 0 || raw > MaxPlausibleRPM {
			return reading{}, false
		}
		return reading{metric: "fan_speed", sensor: identity, value: raw}, true
	default:
		return reading{}, false
	}
}

// chipName builds the stable chip identity for one hwmon device.
//
// See the type comment: the chip's own `name`, plus the device basename when
// it adds something, because two NVMe drives are two chips both called
// `nvme`.
func (s *Sensors) chipName(device string) string {
	name := readSysfsString(filepath.Join(device, "name"))
	if name == "" {
		return ""
	}
	target, err := os.Readlink(filepath.Join(device, "device"))
	if err != nil {
		return name
	}
	instance := filepath.Base(target)
	if instance == "" || instance == "." || normaliseChip(instance) == normaliseChip(name) {
		return name
	}
	return name + "-" + instance
}

// normaliseChip folds the differences that make two spellings of one name
// look distinct: case, and underscore against hyphen.
func normaliseChip(value string) string {
	return strings.ToLower(strings.ReplaceAll(value, "_", "-"))
}

// readSysfsString reads a one-line sysfs attribute, or "".
func readSysfsString(path string) string {
	raw, err := os.ReadFile(path) //nolint:gosec // a path under the configured sys root
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(raw))
}

// readSysfsValue reads a numeric sysfs attribute.
func readSysfsValue(path string) (float64, error) {
	text := readSysfsString(path)
	if text == "" {
		return 0, fmt.Errorf("%s is empty or unreadable", path)
	}
	value, err := strconv.ParseFloat(text, 64)
	if err != nil {
		return 0, fmt.Errorf("%s is not a number: %w", path, err)
	}
	return value, nil
}
