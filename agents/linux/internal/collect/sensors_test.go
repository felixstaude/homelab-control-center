package collect_test

import (
	"context"
	"sort"
	"strings"
	"testing"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/collect"
)

// Sensor fixtures, in testdata:
//
//	sensors       — two NVMe drives with identical chip names and identical
//	                sensor labels, a CPU package and core, a pump and a
//	                stopped fan, a chip whose device basename is its own name
//	                spelled differently, a sensor with no label, a
//	                disconnected thermistor, an unreadable file, and a hwmon
//	                directory with no name
//	sensorsempty  — /sys/class/hwmon exists and is empty: a virtual machine
//	sensorsnone   — no /sys/class/hwmon at all: a container without /sys
const (
	sensorsHost  = "testdata/sensors"
	sensorsEmpty = "testdata/sensorsempty"
	sensorsNone  = "testdata/sensorsnone"
)

func sensorNames(samples []collect.Sample, metric string) []string {
	out := make([]string, 0, len(samples))
	for _, sample := range samples {
		if sample.Metric == metric {
			out = append(out, sample.Labels["sensor"])
		}
	}
	sort.Strings(out)
	return out
}

func sensorValue(t *testing.T, samples []collect.Sample, metric, sensor string) (float64, bool) {
	t.Helper()
	return labelled(samples, metric, "sensor", sensor)
}

func TestTwoIdenticalChipsAreNotMergedIntoOneSeries(t *testing.T) {
	// The defect this is shaped around, found on real hardware: a machine
	// with two NVMe drives has two chips both named `nvme`, each reporting a
	// sensor labelled `Composite`. Keyed on chip and label alone they become
	// one series, which silently averages two drives' temperatures — and
	// averaging is exactly what hides the one drive that is cooking.
	collector := collect.NewSensors(sensorsHost)

	samples, err := collector.Collect(context.Background())

	if err != nil {
		t.Fatalf("collect: %v", err)
	}
	first, foundFirst := sensorValue(t, samples, "temperature", "nvme-nvme0 Composite")
	second, foundSecond := sensorValue(t, samples, "temperature", "nvme-nvme1 Composite")
	if !foundFirst || !foundSecond {
		t.Fatalf("the two drives are not both reported: %v",
			sensorNames(samples, "temperature"))
	}
	if first == second {
		t.Errorf("both drives report %v; they were merged", first)
	}
	if first != 32.85 {
		t.Errorf("nvme0 = %v, want 32.85 (32850 millidegrees)", first)
	}
}

func TestTheDeviceSuffixIsDroppedWhenItAddsNothing(t *testing.T) {
	// `r8169_0_600:00` and its device `r8169-0-600:00` are one name written
	// twice. `r8169_0_600:00-r8169-0-600:00 temp1` helps nobody, so the
	// suffix is suppressed when the two normalise to the same string —
	// a rule that depends only on this chip, never on which others exist.
	collector := collect.NewSensors(sensorsHost)
	samples, err := collector.Collect(context.Background())
	if err != nil {
		t.Fatalf("collect: %v", err)
	}

	if _, found := sensorValue(t, samples, "temperature", "r8169_0_600:00 temp1"); !found {
		t.Errorf("the suffix was not suppressed: %v", sensorNames(samples, "temperature"))
	}
}

func TestASensorWithNoLabelUsesItsChannelNumber(t *testing.T) {
	// `temp1` rather than `hwmon4`: the index is assigned by the driver from
	// the hardware's own channel numbering and is stable for a given chip,
	// while the hwmon number is probe order and changes between boots.
	collector := collect.NewSensors(sensorsHost)
	samples, err := collector.Collect(context.Background())
	if err != nil {
		t.Fatalf("collect: %v", err)
	}

	for _, name := range sensorNames(samples, "temperature") {
		if strings.Contains(name, "hwmon") {
			t.Errorf("a sensor is identified by its hwmon number: %q", name)
		}
	}
	if _, found := sensorValue(t, samples, "temperature", "r8169_0_600:00 temp1"); !found {
		t.Error("an unlabelled sensor was dropped instead of using its channel number")
	}
}

func TestMillidegreesBecomeDegrees(t *testing.T) {
	collector := collect.NewSensors(sensorsHost)
	samples, err := collector.Collect(context.Background())
	if err != nil {
		t.Fatalf("collect: %v", err)
	}

	value, found := sensorValue(t, samples, "temperature", "coretemp-coretemp.0 Package id 0")
	if !found {
		t.Fatal("no package temperature")
	}
	if value != 45 {
		t.Errorf("package = %v, want 45 (45000 millidegrees)", value)
	}
}

func TestAStoppedFanIsReportedAsZeroRatherThanOmitted(t *testing.T) {
	// 0 RPM is a measurement: it is what "the fan is not spinning" looks
	// like, and on a machine with a dead fan it is the most important number
	// on the page. Omitting it would make a failed fan indistinguishable
	// from a fan nobody is watching.
	collector := collect.NewSensors(sensorsHost)
	samples, err := collector.Collect(context.Background())
	if err != nil {
		t.Fatalf("collect: %v", err)
	}

	value, found := sensorValue(t, samples, "fan_speed", "z53-0003:1E71:3008.0006 Fan speed")
	if !found {
		t.Fatalf("a stopped fan was omitted: %v", sensorNames(samples, "fan_speed"))
	}
	if value != 0 {
		t.Errorf("stopped fan = %v, want 0", value)
	}
	if pump, _ := sensorValue(t, samples, "fan_speed", "z53-0003:1E71:3008.0006 Pump speed"); pump != 1190 {
		t.Errorf("pump = %v, want 1190", pump)
	}
}

func TestADisconnectedThermistorIsDroppedRatherThanClamped(t *testing.T) {
	// -273.2 °C is what a disconnected sensor reads. Clamping it to -40
	// would turn a broken sensor into a believable one; keeping it would
	// give every chart on the page a useless y-axis, which hides the real
	// readings beside it.
	collector := collect.NewSensors(sensorsHost)
	samples, err := collector.Collect(context.Background())
	if err != nil {
		t.Fatalf("collect: %v", err)
	}

	if _, found := sensorValue(t, samples, "temperature", "acpitz Disconnected"); found {
		t.Error("a -273 °C reading was reported")
	}
	for _, sample := range samples {
		if sample.Metric == "temperature" && sample.Value < collect.MinPlausibleCelsius {
			t.Errorf("an implausible temperature survived: %+v", sample)
		}
	}
}

func TestOneBrokenSensorDoesNotHideTheRestOfTheChip(t *testing.T) {
	// A chip that is asleep, a drive that has spun down, a busy I²C bus:
	// unreadable sensors are ordinary. Failing the collection over one would
	// blank out every temperature on the host.
	collector := collect.NewSensors(sensorsHost)

	samples, err := collector.Collect(context.Background())

	if err != nil {
		t.Fatalf("one unreadable sensor failed the whole collection: %v", err)
	}
	if _, found := sensorValue(t, samples, "temperature", "acpitz Broken"); found {
		t.Error("an unparseable reading was reported")
	}
	if value, found := sensorValue(t, samples, "temperature", "acpitz Working"); !found || value != 41 {
		t.Errorf("the working sensor on the same chip was lost: %v found=%v", value, found)
	}
}

func TestAChipWithNoNameIsSkippedRatherThanNumbered(t *testing.T) {
	// Falling back to `hwmon6` would be the unstable positional key the
	// whole identity scheme exists to avoid — and it would look fine until
	// the next reboot renumbered it.
	collector := collect.NewSensors(sensorsHost)
	samples, err := collector.Collect(context.Background())
	if err != nil {
		t.Fatalf("collect: %v", err)
	}

	for _, name := range sensorNames(samples, "temperature") {
		if strings.HasPrefix(name, "hwmon") {
			t.Errorf("a chip with no name was given a positional identity: %q", name)
		}
	}
}

func TestEverySensorValueIsFinite(t *testing.T) {
	collector := collect.NewSensors(sensorsHost)
	samples, err := collector.Collect(context.Background())
	if err != nil {
		t.Fatalf("collect: %v", err)
	}
	for _, sample := range samples {
		if !sample.Valid() {
			t.Errorf("invalid sample reached the output: %+v", sample)
		}
	}
}

// --- Availability -----------------------------------------------------------

func TestAVirtualMachineWithNoSensorsSaysSo(t *testing.T) {
	// The common case, and the one where inventing a number would be most
	// tempting and most wrong.
	collector := collect.NewSensors(sensorsEmpty)

	availability := collector.Available()

	if availability.Available {
		t.Fatal("a host with no sensors reported itself available")
	}
	if !strings.Contains(availability.Reason, "no readable temperature or fan sensors") {
		t.Errorf("the reason does not explain itself: %q", availability.Reason)
	}
}

func TestAHostWithNoHwmonAtAllIsADifferentAnswer(t *testing.T) {
	// A container without /sys, which calls for a different action from a
	// machine that has the subsystem and no sensors on it.
	collector := collect.NewSensors(sensorsNone)

	availability := collector.Available()

	if availability.Available {
		t.Fatal("a host with no hwmon reported itself available")
	}
	if !strings.Contains(availability.Reason, "does not exist") {
		t.Errorf("the reason does not distinguish absence: %q", availability.Reason)
	}
}

func TestAHostWithSensorsIsAvailable(t *testing.T) {
	if availability := collect.NewSensors(sensorsHost).Available(); !availability.Available {
		t.Errorf("a host with sensors reported unavailable: %q", availability.Reason)
	}
}

func TestTheSensorCollectorIdentifiesItself(t *testing.T) {
	if got := collect.NewSensors("").Module(); got != "sensors" {
		t.Errorf("sensors module = %q", got)
	}
}

func TestThresholdFilesAreNotStoredAsMeasurements(t *testing.T) {
	// temp1_crit is a constant. Storing one per interval would be the same
	// number a million times, and the data model has no place for a
	// constant — an interface that wants "32 °C of 95 °C critical" needs a
	// mechanism that does not exist yet.
	collector := collect.NewSensors(sensorsHost)
	samples, err := collector.Collect(context.Background())
	if err != nil {
		t.Fatalf("collect: %v", err)
	}

	for _, sample := range samples {
		if sample.Value == 94.85 {
			t.Errorf("a critical threshold was stored as a reading: %+v", sample)
		}
	}
}
