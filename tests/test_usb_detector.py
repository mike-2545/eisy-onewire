from __future__ import annotations

import ctypes
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from eisy_onewire.usb_detector import (  # noqa: E402
    CtypesLibUsbBackend,
    DS9490_PRODUCT_ID,
    DS9490_VENDOR_ID,
    DetectionStatus,
    LIBUSB_ERROR_ACCESS,
    _LibUsbDeviceDescriptor,
    detect_ds9490,
)


class FakeLibUsb:
    def __init__(
        self,
        devices: list[dict[str, int]],
        *,
        init_code: int = 0,
        list_code: int | None = None,
        descriptor_code: int = 0,
        open_code: int = 0,
    ) -> None:
        self.devices = devices
        self.init_code = init_code
        self.list_code = len(devices) if list_code is None else list_code
        self.descriptor_code = descriptor_code
        self.open_code = open_code
        self.open_calls: list[int] = []
        self.close_calls: list[int] = []
        self.free_calls = 0
        self.exit_calls = 0
        self._device_array = (ctypes.c_void_p * max(1, len(devices)))(
            *(index + 1 for index in range(len(devices)))
        )

    def libusb_init(self, context_out: object) -> int:
        ctypes.cast(context_out, ctypes.POINTER(ctypes.c_void_p))[0] = 99
        return self.init_code

    def libusb_exit(self, context: ctypes.c_void_p) -> None:
        self.exit_calls += 1

    def libusb_get_device_list(self, context: object, list_out: object) -> int:
        list_pointer = ctypes.cast(self._device_array, ctypes.POINTER(ctypes.c_void_p))
        ctypes.cast(
            list_out, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
        )[0] = list_pointer
        return self.list_code

    def libusb_free_device_list(self, device_list: object, unref: int) -> None:
        self.free_calls += 1

    def libusb_get_device_descriptor(self, device: int, descriptor_out: object) -> int:
        if self.descriptor_code != 0:
            return self.descriptor_code
        values = self.devices[int(device) - 1]
        descriptor = ctypes.cast(
            descriptor_out, ctypes.POINTER(_LibUsbDeviceDescriptor)
        )[0]
        descriptor.bLength = 18
        descriptor.bDescriptorType = 1
        descriptor.bcdUSB = values.get("usb_version", 0x0200)
        descriptor.bDeviceClass = values.get("device_class", 0)
        descriptor.bDeviceSubClass = values.get("device_subclass", 0)
        descriptor.bDeviceProtocol = values.get("device_protocol", 0)
        descriptor.bMaxPacketSize0 = values.get("max_packet_size0", 8)
        descriptor.idVendor = values["vendor_id"]
        descriptor.idProduct = values["product_id"]
        descriptor.bcdDevice = values.get("device_version", 0x0002)
        descriptor.bNumConfigurations = values.get("num_configurations", 1)
        return 0

    def libusb_get_bus_number(self, device: int) -> int:
        return self.devices[int(device) - 1].get("bus_number", 0)

    def libusb_get_device_address(self, device: int) -> int:
        return self.devices[int(device) - 1].get("device_address", 0)

    def libusb_open(self, device: int, handle_out: object) -> int:
        self.open_calls.append(int(device))
        if self.open_code == 0:
            ctypes.cast(handle_out, ctypes.POINTER(ctypes.c_void_p))[0] = 1000 + int(
                device
            )
        return self.open_code

    def libusb_close(self, handle: ctypes.c_void_p) -> None:
        self.close_calls.append(handle.value)


def device(vendor_id: int, product_id: int, **values: int) -> dict[str, int]:
    return {"vendor_id": vendor_id, "product_id": product_id, **values}


class UsbDetectorTests(unittest.TestCase):
    def test_absent_when_only_unrelated_devices_exist(self) -> None:
        fake = FakeLibUsb([device(0x1234, 0x5678)])

        result = detect_ds9490(CtypesLibUsbBackend(fake))

        self.assertEqual(result.status, DetectionStatus.ABSENT)
        self.assertIsNone(result.descriptor)
        self.assertEqual(fake.open_calls, [])
        self.assertEqual(fake.free_calls, 1)
        self.assertEqual(fake.exit_calls, 1)

    def test_permission_denied_preserves_matching_descriptor(self) -> None:
        fake = FakeLibUsb(
            [
                device(
                    DS9490_VENDOR_ID,
                    DS9490_PRODUCT_ID,
                    bus_number=2,
                    device_address=7,
                )
            ],
            open_code=LIBUSB_ERROR_ACCESS,
        )

        result = detect_ds9490(CtypesLibUsbBackend(fake))

        self.assertEqual(result.status, DetectionStatus.PERMISSION_DENIED)
        self.assertEqual(result.error_code, LIBUSB_ERROR_ACCESS)
        self.assertIsNotNone(result.descriptor)
        self.assertEqual(result.descriptor.vendor_id, DS9490_VENDOR_ID)
        self.assertEqual(result.descriptor.product_id, DS9490_PRODUCT_ID)
        self.assertEqual(result.descriptor.bus_number, 2)
        self.assertEqual(result.descriptor.device_address, 7)
        self.assertEqual(fake.close_calls, [])

    def test_success_opens_and_immediately_closes_matching_device(self) -> None:
        fake = FakeLibUsb(
            [
                device(0x1111, 0x2222),
                device(
                    DS9490_VENDOR_ID,
                    DS9490_PRODUCT_ID,
                    usb_version=0x0110,
                    max_packet_size0=8,
                    bus_number=1,
                    device_address=4,
                ),
            ]
        )

        result = detect_ds9490(CtypesLibUsbBackend(fake))

        self.assertEqual(result.status, DetectionStatus.OPENED)
        self.assertEqual(fake.open_calls, [2])
        self.assertEqual(fake.close_calls, [1002])
        self.assertEqual(result.descriptor.usb_version, 0x0110)
        self.assertEqual(result.descriptor.max_packet_size0, 8)

    def test_unexpected_open_error_is_distinct(self) -> None:
        fake = FakeLibUsb(
            [device(DS9490_VENDOR_ID, DS9490_PRODUCT_ID)], open_code=-6
        )

        result = detect_ds9490(CtypesLibUsbBackend(fake))

        self.assertEqual(result.status, DetectionStatus.UNEXPECTED_ERROR)
        self.assertEqual(result.error_code, -6)
        self.assertIsNotNone(result.descriptor)
        self.assertEqual(fake.close_calls, [])

    def test_initialization_error_does_not_attempt_enumeration_cleanup(self) -> None:
        fake = FakeLibUsb([], init_code=-99)

        result = detect_ds9490(CtypesLibUsbBackend(fake))

        self.assertEqual(result.status, DetectionStatus.UNEXPECTED_ERROR)
        self.assertEqual(result.error_code, -99)
        self.assertEqual(fake.free_calls, 0)
        self.assertEqual(fake.exit_calls, 0)

    def test_enumeration_error_exits_context_without_freeing_missing_list(self) -> None:
        fake = FakeLibUsb([], list_code=-5)

        result = detect_ds9490(CtypesLibUsbBackend(fake))

        self.assertEqual(result.status, DetectionStatus.UNEXPECTED_ERROR)
        self.assertEqual(result.error_code, -5)
        self.assertEqual(fake.free_calls, 0)
        self.assertEqual(fake.exit_calls, 1)

    def test_descriptor_error_is_unexpected_and_never_opens(self) -> None:
        fake = FakeLibUsb(
            [device(DS9490_VENDOR_ID, DS9490_PRODUCT_ID)], descriptor_code=-1
        )

        result = detect_ds9490(CtypesLibUsbBackend(fake))

        self.assertEqual(result.status, DetectionStatus.UNEXPECTED_ERROR)
        self.assertEqual(result.error_code, -1)
        self.assertEqual(fake.open_calls, [])

    def test_default_backend_on_windows_returns_an_error_result(self) -> None:
        if sys.platform.startswith("freebsd"):
            self.skipTest("this assertion is specific to non-FreeBSD test hosts")

        result = detect_ds9490()

        self.assertEqual(result.status, DetectionStatus.UNEXPECTED_ERROR)
        self.assertIn("only on FreeBSD", result.detail)


if __name__ == "__main__":
    unittest.main()

