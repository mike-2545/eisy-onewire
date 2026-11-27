from __future__ import annotations

import unittest
from pathlib import Path

from eisy_onewire.node_identity import controller_address, sensor_address
from eisy_onewire.sensor_config import (
    DS2409_BRANCH_READ_EVENT_POLICY_KEY,
    DS2409_EVENT_POLICY_KEY,
    DS2409_UNATTENDED_BRANCH_POLICY_KEY,
    Ds2409EventFlagPolicy,
    Ds2409UnattendedBranchPolicy,
    SensorConfigurationError,
    load_default_parameters,
    parse_ds2409_event_flag_policy,
    parse_ds2409_branch_read_event_flag_policy,
    parse_ds2409_unattended_branch_policy,
    parse_ds2438_applications,
    parse_friendly_name_overrides,
    parse_sensor_parameters,
)

ROOT = Path(__file__).resolve().parents[1]
SENSOR_A_ROM = bytes.fromhex("2811223344556656")


class SensorConfigTests(unittest.TestCase):
    def test_ds2438_requires_an_explicit_crc_valid_application_mapping(self) -> None:
        parameters = {
            "_config_version": "1",
            "_ds2438_application_2666778899AABB54": "hih4000",
        }
        configured = parse_ds2438_applications(parameters)
        self.assertEqual(configured[0].rom, bytes.fromhex("2666778899AABB54"))
        self.assertEqual(configured[0].address, "2666778899aabb")
        self.assertEqual(parse_sensor_parameters(parameters), ())
        for value in ("unknown", "", 1):
            with self.subTest(value=value), self.assertRaises(SensorConfigurationError):
                parse_ds2438_applications(
                    {"_ds2438_application_2666778899AABB54": value}
                )

        unassigned = parse_ds2438_applications(
            {"_ds2438_application_2666778899AABB54": "unassigned"}
        )
        self.assertEqual(unassigned[0].application.value, "unassigned")

    def test_analog_and_solar_applications_require_only_relevant_calibration(self) -> None:
        analog = parse_ds2438_applications(
            {"_ds2438_application_2666778899AABB54": "analog_voltage"}
        )[0]
        self.assertEqual(analog.application.value, "analog_voltage")
        self.assertIsNone(analog.application_error)

        missing = parse_ds2438_applications(
            {"_ds2438_application_2666778899AABB54": "solar_irradiance"}
        )[0]
        self.assertIsNone(missing.solar_calibration)
        self.assertIn("positive W/m2/V", missing.application_error)

        calibrated = parse_ds2438_applications(
            {
                "_ds2438_application_2666778899aabb54": "solar_irradiance",
                "_ds2438_solar_multiplier_2666778899AABB54": "250",
                "_ds2438_solar_offset_2666778899AABB54": "-1.5",
            }
        )[0]
        self.assertEqual(calibrated.solar_calibration.multiplier_w_m2_per_volt, 250)
        self.assertEqual(calibrated.solar_calibration.offset_w_m2, -1.5)

        # Invalid solar calibration is isolated to that device rather than
        # invalidating an unrelated, explicitly selected analog sensor.
        mixed = parse_ds2438_applications(
            {
                "_ds2438_application_2666778899AABB54": "solar_irradiance",
                "_ds2438_solar_multiplier_2666778899AABB54": "bad",
                "_ds2438_application_2655443322110061": "analog_voltage",
            }
        )
        self.assertEqual(len(mixed), 2)
        self.assertEqual(sum(item.application_error is not None for item in mixed), 1)

    def test_optional_names_accept_both_supported_families_and_isolate_errors(self) -> None:
        ds2438 = bytes.fromhex("2666778899AABB54")
        previous, errors = parse_friendly_name_overrides(
            {
                SENSOR_A_ROM.hex().upper(): "Probe A",
                ds2438.hex().upper(): "Humidity",
            }
        )
        self.assertEqual(errors, ())
        self.assertEqual({item.rom for item in previous}, {SENSOR_A_ROM, ds2438})

        retained, errors = parse_friendly_name_overrides(
            {ds2438.hex().upper(): "   ", "bad": "ignored"},
            previous={item.rom: item for item in previous},
        )
        self.assertEqual({item.rom: item.name for item in retained}, {ds2438: "Humidity"})
        self.assertEqual(len(errors), 2)

    def test_packaged_defaults_are_safe_and_contain_no_personal_sensor(self) -> None:
        defaults = load_default_parameters(ROOT / "default-sensors.json")
        configured = parse_sensor_parameters(defaults)

        self.assertEqual(defaults[DS2409_EVENT_POLICY_KEY], "preserve")
        self.assertEqual(defaults[DS2409_BRANCH_READ_EVENT_POLICY_KEY], "preserve")
        self.assertEqual(defaults[DS2409_UNATTENDED_BRANCH_POLICY_KEY], "disabled")
        self.assertIs(
            parse_ds2409_event_flag_policy(defaults),
            Ds2409EventFlagPolicy.PRESERVE,
        )
        self.assertEqual(configured, ())
        self.assertEqual(controller_address(), "onewire")
        self.assertEqual(sensor_address(SENSOR_A_ROM), "28112233445566")

    def test_friendly_name_is_editable_without_changing_identity(self) -> None:
        first = parse_sensor_parameters({"_config_version": "1", "2811223344556656": "Test Sensor"})[0]
        renamed = parse_sensor_parameters({"_config_version": "1", "2811223344556656": "Garden"})[0]
        self.assertEqual(first.address, renamed.address)
        self.assertEqual(first.rom, renamed.rom)
        self.assertEqual(renamed.name, "Garden")

    def test_multiple_sensors_have_distinct_stable_addresses(self) -> None:
        configured = parse_sensor_parameters(
            {
                "_config_version": "1",
                "2811223344556656": "One",
                "2822334455667727": "Two",
                "28334455667788E7": "Three",
            }
        )
        self.assertEqual(
            {item.address for item in configured},
            {"28112233445566", "28223344556677", "28334455667788"},
        )

    def test_invalid_rom_unsupported_family_missing_and_duplicate_names_fail_atomically(self) -> None:
        invalid = (
            {"_config_version": "1", "bad": "Name"},
            {"_config_version": "1", "1F5566778899AA38": "Coupler"},
            {"_config_version": "1", "2811223344556656": "   "},
            {
                "_config_version": "1",
                "2811223344556656": "Same",
                "2822334455667727": "same",
            },
        )
        for parameters in invalid:
            with self.subTest(parameters=parameters):
                with self.assertRaises(SensorConfigurationError):
                    parse_sensor_parameters(parameters)

    def test_future_adapter_namespace_is_deterministic_without_changing_default_bus(self) -> None:
        other_controller = controller_address("814455667788999C")
        other_sensor = sensor_address(SENSOR_A_ROM, "814455667788999C")
        self.assertEqual(len(other_controller), 14)
        self.assertEqual(len(other_sensor), 14)
        self.assertNotEqual(other_controller, "onewire")
        self.assertNotEqual(other_sensor, "28112233445566")
        self.assertEqual(other_sensor, sensor_address(SENSOR_A_ROM, "814455667788999C"))

    def test_ds2409_event_flag_acknowledgement_requires_exact_opt_in(self) -> None:
        self.assertIs(
            parse_ds2409_event_flag_policy({"_config_version": "1"}),
            Ds2409EventFlagPolicy.PRESERVE,
        )
        self.assertIs(
            parse_ds2409_event_flag_policy(
                {
                    "_config_version": "1",
                    DS2409_EVENT_POLICY_KEY: "acknowledge",
                }
            ),
            Ds2409EventFlagPolicy.ACKNOWLEDGE,
        )
        for invalid in (True, "yes", "", "clear"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(SensorConfigurationError):
                    parse_sensor_parameters(
                        {
                            "_config_version": "1",
                            DS2409_EVENT_POLICY_KEY: invalid,
                        }
                    )

    def test_branch_read_event_permission_is_distinct_and_default_off(self) -> None:
        values = {
            "_config_version": "1",
            DS2409_EVENT_POLICY_KEY: "acknowledge",
        }
        self.assertIs(
            parse_ds2409_branch_read_event_flag_policy(values),
            Ds2409EventFlagPolicy.PRESERVE,
        )
        values[DS2409_BRANCH_READ_EVENT_POLICY_KEY] = "acknowledge"
        self.assertIs(
            parse_ds2409_branch_read_event_flag_policy(values),
            Ds2409EventFlagPolicy.ACKNOWLEDGE,
        )
        self.assertIs(
            parse_ds2409_event_flag_policy(
                {
                    "_config_version": "1",
                    DS2409_BRANCH_READ_EVENT_POLICY_KEY: "acknowledge",
                }
            ),
            Ds2409EventFlagPolicy.PRESERVE,
        )
        with self.assertRaises(SensorConfigurationError):
            parse_sensor_parameters(
                {
                    "_config_version": "1",
                    DS2409_BRANCH_READ_EVENT_POLICY_KEY: "yes",
                }
            )

    def test_unattended_policy_is_disabled_and_independent_by_default(self) -> None:
        manual_permissions = {
            "_config_version": "1",
            DS2409_EVENT_POLICY_KEY: "acknowledge",
            DS2409_BRANCH_READ_EVENT_POLICY_KEY: "acknowledge",
        }
        self.assertIs(
            parse_ds2409_unattended_branch_policy(manual_permissions),
            Ds2409UnattendedBranchPolicy.DISABLED,
        )
        for value, expected in (
            ("preserve", Ds2409UnattendedBranchPolicy.PRESERVE),
            ("acknowledge", Ds2409UnattendedBranchPolicy.ACKNOWLEDGE),
        ):
            configured = dict(manual_permissions)
            configured[DS2409_UNATTENDED_BRANCH_POLICY_KEY] = value
            self.assertIs(parse_ds2409_unattended_branch_policy(configured), expected)
        with self.assertRaises(SensorConfigurationError):
            parse_sensor_parameters(
                {
                    "_config_version": "1",
                    DS2409_UNATTENDED_BRANCH_POLICY_KEY: "yes",
                }
            )


if __name__ == "__main__":
    unittest.main()
