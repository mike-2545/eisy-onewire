# Using the DS2438 as an analog-to-1-Wire interface

The Dallas/Maxim DS2438 Smart Battery Monitor was designed for battery packs,
but its general-purpose voltage input, supply-voltage measurement, temperature
sensor, unique 1-Wire ROM, and multidrop interface also make it useful as a
small analog-to-1-Wire building block. With appropriate external circuitry, a
conventional voltage-output sensor can be read over the same 1-Wire network as
other supported devices.

This is an application guide, not a construction drawing or a substitute for
the component manufacturers' installation instructions. Verify the exact
sensor, supply, protection, grounding, and calibration for each assembly before
connecting it to a DS2438 or a 1-Wire network.

## What the DS2438 provides

The DS2438 combines these useful functions:

- a unique family-`0x26` 64-bit 1-Wire ROM;
- a general-purpose voltage input, **VAD**;
- a selectable measurement of **VAD** or the device supply, **VDD**;
- an on-chip temperature measurement; and
- battery-current inputs and accumulators that eisy-onewire does not use for
  the analog applications described here.

The voltage converter is 10-bit. Its register covers 0 to 10.23 V in 10 mV
steps, while the data sheet describes the conversion range as 0 to 10 V. The
manufacturer's guaranteed VAD accuracy range is narrower: 1.5 to 10 V, subject
to the stated VDD conditions. The specified voltage errors are typically
plus-or-minus 25 mV and at most plus-or-minus 75 mV for VAD, and typically
plus-or-minus 10 mV and at most plus-or-minus 50 mV for VDD. Conversion time is
typically 3 ms and at most 10 ms.

Consequently, a code below 1.5 V can still be useful evidence—for example, a
dark output from a light sensor—but it is outside the guaranteed VAD accuracy
range. Resolution is not the same as accuracy: displaying two decimal places
matches the 10 mV code step but does not make the measurement accurate to
0.01 V.

The on-chip thermometer has 0.03125 °C resolution over its encoded -55 to
+125 °C measurement range. The DS2438 itself is specified to operate from
-40 to +85 °C, and the thermometer error is specified as plus-or-minus 2 °C.
That temperature is the temperature of the DS2438 package, not necessarily the
ambient temperature at a remote sensing element.

## Manufacturer pinout

The following pinout is for the DS2438Z/DS2438AZ 8-pin SOIC and is taken from
the [Analog Devices DS2438 data sheet](https://www.analog.com/media/en/technical-documentation/data-sheets/ds2438.pdf).

| Pin | Name | Function in an analog interface |
| --- | --- | --- |
| 1 | GND | Common reference for the DS2438, sensor output, and supply. |
| 2 | VSENS+ | Battery-current monitor positive input; not used by the eisy-onewire analog applications. |
| 3 | VSENS- | Battery-current monitor negative input; not used by the eisy-onewire analog applications. |
| 4 | VAD | General-purpose voltage ADC input. |
| 5 | VDD | DS2438 supply input and selectable supply-voltage measurement. |
| 6, 7 | NC | No connect; do not use as tie points. |
| 8 | DQ | Open-drain 1-Wire data input/output. |

Check package orientation against the manufacturer's package drawing before
wiring. A reversed SOIC pinout can damage the DS2438 and the attached sensor.

## Electrical limits

The distinction between a normal operating condition and an absolute maximum
is essential.

| Item | Manufacturer value | Design meaning |
| --- | --- | --- |
| VDD operating range | 2.4 to 10.0 V | Provide a regulated, adequately bypassed supply in this range. |
| DQ pull-up | 2.4 to 5.5 V | Use the 1-Wire master's supported bus voltage and topology. |
| Voltage ADC coding | 0 to 10.23 V, 10 mV/LSB | Codes exist across this span; the described conversion range is 0 to 10 V. |
| Guaranteed VAD input range | 1.5 to 10.0 V | VAD readings below 1.5 V are outside the guaranteed accuracy table. |
| VAD accuracy condition | `1.5 V < VAD < 2 × VDD`, with `2.4 V < VDD < 5.0 V` | At VDD = 5 V, the data sheet describes specified accuracy over the full 1.5–10 V VAD range. |
| VDD/VAD absolute maximum | -0.3 to +12 V | Stress limit only—not an operating target. |
| Other-pin absolute maximum | -0.3 to +7 V | Includes DQ; normal DQ operation is more restrictive. |
| VSENS differential absolute maximum | less than plus-or-minus 300 mV | These pins are not general-purpose voltage inputs. |
| Device operating temperature | -40 to +85 °C | Stay inside this range even though the temperature register encodes a wider span. |

Operating at an absolute maximum can damage the part or reduce reliability.
Never connect mains, an unbounded battery, an automotive rail, an inductive
load, or an unknown sensor output directly to VAD.

The DS2438 can steal enough power from DQ to make its ROM readable when normal
power is absent. The manufacturer does not present that as a substitute for
VDD during analog conversions. An analog interface therefore needs a proper
VDD supply; do not design it as a parasite-powered sensor.

## Basic functional block diagram

```text
 conventional analog sensor
       |  voltage output
       v
 protection / scaling / filtering / optional buffer
       |
       +-------------------------------> DS2438 VAD (pin 4)

 regulated sensor supply --------------> DS2438 VDD (pin 5)
       |                                      |
       +----> sensor supply                    +-- selectable VDD measurement

 common ground ------------------------> DS2438 GND (pin 1)
 1-Wire data + pull-up <---------------> DS2438 DQ  (pin 8)
```

The sensor and DS2438 normally share a ground so that VAD has a defined
reference. If isolation is required, it must be designed explicitly on both
the power and signal paths.

## Signal conditioning

The external circuit is responsible for presenting a safe, stable voltage to
VAD. Depending on the sensor, that may require:

- **Scaling.** Use a resistor divider or amplifier so the worst-case signal,
  including faults and supply tolerance, stays within the operating range.
  Analog Devices' high-voltage DS2438 application note uses a divider for VAD
  and warns that divider impedance must be low enough that input loading does
  not create an unacceptable error.
- **Buffering.** A high-output-impedance sensor may need a unity-gain buffer or
  amplifier. Confirm input common-mode range, output swing, offset, bias
  current, noise, and behavior during power-up and power-down.
- **Filtering.** A small RC low-pass filter can reduce noise, but its source
  impedance and settling time must still allow a valid conversion. Do not copy
  a component value without calculating it for the actual sensor and sample
  rate.
- **Protection.** Consider current limiting, clamps, ESD protection, reverse
  polarity, and transient protection appropriate to the wiring environment.
  Analog Devices specifically discusses DQ ESD protection, input filtering,
  and VDD bypassing in its application note.
- **Supply regulation and bypassing.** Keep VDD within 2.4–10 V under every
  condition and place local decoupling close to the device. If the sensor is
  ratiometric, measure the same supply through VDD rather than assuming an
  ideal nominal voltage.
- **Calibration.** Divider tolerance, amplifier gain/offset, cable resistance,
  sensor tolerance, DS2438 ADC error, temperature, and aging all contribute to
  total error. Calibrate the complete assembled signal chain when accuracy
  matters.

For inputs above 10 V, Analog Devices shows regulated VDD plus a VAD divider in
its [high-voltage battery-pack application note](https://www.analog.com/en/resources/technical-articles/2022/07/16/06/38/using-dallas-battery-management-ics-with-high-voltage-battery-packs.html).
That circuit is a design reference, not a drop-in schematic for arbitrary
sensors.

## Application patterns

### Humidity: ratiometric voltage output

```text
 regulated supply +----> humidity sensor supply
                  +----> DS2438 VDD

 humidity sensor output ----------------> DS2438 VAD
 common ground --------------------------> DS2438 GND
 DS2438 package temperature ------------> compensation calculation
```

This pattern is live-tested in eisy-onewire for an explicitly identified
Honeywell HIH-4000 application. The HIH-4000 is ratiometric, so the calculation
uses both VAD and VDD. Its official nominal functions are:

```text
sensor_rh = ((VAD / VDD) - 0.16) / 0.0062
true_rh   = sensor_rh / (1.0546 - 0.00216 * temperature_c)
```

The [Honeywell HIH-4000 product sheet](https://prod-edam.honeywell.com/content/dam/honeywell-edam/sps/siot/en-gb/products/sensors/humidity-with-temperature-sensors/hih-4000-series/documents/sps-siot-hih4000-series-product-sheet-009017-5-en-ciid-49922.pdf)
specifies a 4–5.8 V supply, calibration at 5 V and 25 °C, and a minimum 80 kΩ
load. Its nominal transfer function is not valid for an unidentified humidity
element. eisy-onewire therefore leaves an unassigned DS2438 inventory-only
rather than inferring a humidity model from its voltage.

### Solar radiation: amplified pyranometer

```text
 pyranometer sensing element
       |
       v
 manufacturer/interface amplifier and scaling
       |
       +-------------------------------> DS2438 VAD

 interface supply ---------------------> DS2438 VDD

 software: irradiance = VAD * multiplier + offset
```

The generic architecture is live-tested: eisy-onewire has acquired a
root-bus DS2438's raw VAD/VDD values, demonstrated a dark-to-illuminated VAD
response, and verified the configured linear calculation. That test did **not**
establish absolute irradiance accuracy.

Pyranometer generations and interface boards can use different sensitivities,
supplies, offsets, and scaling. Use the calibration certificate or
manufacturer documentation for the complete current instrument. Do not infer
a multiplier from a model family, enclosure, approximate date, or flashlight
test.

### Generic analog voltage

For a sensor that already provides a safe voltage, eisy-onewire's
`analog_voltage` application publishes VAD and VDD without applying a humidity
or irradiance formula. This path is live-tested for manual and scheduled
measurements. It remains the operator's responsibility to translate that
voltage into engineering units and to ensure the electrical interface is safe.

## Legacy multi-sensor weather-interface style

Some older environmental instruments combine several ordinary sensors with a
small interface board: one channel may condition an amplified pyranometer,
another may condition a voltage-output humidity element, and a separate
DS18B20 may provide environmental temperature. One DS2438 per analog channel
then exposes VAD, VDD, and chip temperature over 1-Wire.

```text
 light sensor ----> amplifier/scaling ----> DS2438 A ----> 1-Wire
 humidity sensor -> conditioning ---------> DS2438 B ----> 1-Wire
 environmental temperature --------------> DS18B20  ----> 1-Wire
                       shared regulated supply and ground
```

This is the observed **functional architecture**, not a reverse-engineered
proprietary schematic. The exact component values, protection network,
amplifier topology, board gain, calibration, and humidity-element model are
unknown unless established from authoritative documentation or direct circuit
analysis. A responsive voltage channel does not by itself identify the sensor
or prove calibration.

## Potential future applications

The same pattern could support other voltage-output transducers, but the
following are conceptual hardware examples—not current eisy-onewire
applications:

| Example | Typical interface questions that must be answered first |
| --- | --- |
| Barometric or fluid pressure | Is the output ratiometric, amplified, differential, or a current loop? What excitation and calibration are required? |
| Soil moisture | Is the probe resistive, capacitive, frequency-output, or voltage-output? How will corrosion, salinity, and soil-specific calibration be handled? |
| Liquid level | Does the transducer provide a safe voltage, 4–20 mA, resistance, or a switched output? Is isolation required? |
| Ambient light intensity | What is the spectral response and transfer function? Does the sensor require a transimpedance amplifier? |
| Battery voltage | What divider, regulation, transient protection, and calibration keep both VDD and VAD safe over charge and fault conditions? |

A 4–20 mA transmitter, bridge sensor, thermocouple, photodiode, resistance
probe, or frequency-output sensor cannot be connected directly merely because
it is “analog.” It needs the correct excitation and conversion circuit.

## What eisy-onewire supports today

| Application | Hardware evidence | Plugin status |
| --- | --- | --- |
| Honeywell HIH-4000 humidity | Manual and unattended live measurements, including branch restoration safeguards | Supported only after explicit `hih4000` identification. |
| Generic VAD/VDD | Manual and scheduled root-bus live measurements | Supported through explicit `analog_voltage` selection. |
| Linear solar irradiance | Raw light response and configured arithmetic live-tested; absolute calibration not verified | Supported through explicit `solar_irradiance` selection and per-device multiplier/offset. |
| Other voltage-output sensors | Conceptual only | No application-specific IoX conversion or validation yet. |

Every newly discovered DS2438 starts as `unassigned`. Selecting an application
is an explicit statement about the attached circuit; the plugin never infers
one from family code or measured voltage. See the
[configuration reference](../POLYGLOT_CONFIG.md#ds2438-applications) and
[supported-hardware summary](supported-hardware.md) before enabling a node.

## Manufacturer references

- [Analog Devices, *DS2438 Smart Battery Monitor Data Sheet*](https://www.analog.com/media/en/technical-documentation/data-sheets/ds2438.pdf)
- [Analog Devices, *Using Dallas Battery Management ICs with High Voltage Battery Packs*](https://www.analog.com/en/resources/technical-articles/2022/07/16/06/38/using-dallas-battery-management-ics-with-high-voltage-battery-packs.html)
- [Honeywell, *HIH-4000 Series Humidity Sensors Product Sheet*](https://prod-edam.honeywell.com/content/dam/honeywell-edam/sps/siot/en-gb/products/sensors/humidity-with-temperature-sensors/hih-4000-series/documents/sps-siot-hih4000-series-product-sheet-009017-5-en-ciid-49922.pdf)
