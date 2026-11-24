"""Read-only DS9490 detection using FreeBSD's native libusb API.

The native boundary is deliberately small and injectable. Production code uses
``ctypes.CDLL`` while tests provide a fake object with the same libusb
functions. Detection does not claim an interface or send USB transfers.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

DS9490_VENDOR_ID = 0x04FA
DS9490_PRODUCT_ID = 0x2490

LIBUSB_SUCCESS = 0
LIBUSB_ERROR_ACCESS = -3


class DetectionStatus(str, Enum):
    """Possible outcomes of a DS9490 access probe."""

    ABSENT = "absent"
    PERMISSION_DENIED = "permission_denied"
    OPENED = "opened"
    UNEXPECTED_ERROR = "unexpected_error"


@dataclass(frozen=True, slots=True)
class UsbDeviceDescriptor:
    """The non-sensitive fields inspected from a matching USB descriptor."""

    vendor_id: int
    product_id: int
    usb_version: int
    device_version: int
    device_class: int
    device_subclass: int
    device_protocol: int
    max_packet_size0: int
    num_configurations: int
    bus_number: int
    device_address: int


@dataclass(frozen=True, slots=True)
class DetectionResult:
    """A stable result independent of the underlying libusb representation."""

    status: DetectionStatus
    descriptor: UsbDeviceDescriptor | None = None
    error_code: int | None = None
    detail: str | None = None


class DetectorBackend(Protocol):
    """Backend contract used to isolate native calls in tests."""

    def probe(self, vendor_id: int, product_id: int) -> DetectionResult:
        """Inspect, open, and close the first device matching both identifiers."""


class _LibUsbDeviceDescriptor(ctypes.Structure):
    _fields_ = [
        ("bLength", ctypes.c_uint8),
        ("bDescriptorType", ctypes.c_uint8),
        ("bcdUSB", ctypes.c_uint16),
        ("bDeviceClass", ctypes.c_uint8),
        ("bDeviceSubClass", ctypes.c_uint8),
        ("bDeviceProtocol", ctypes.c_uint8),
        ("bMaxPacketSize0", ctypes.c_uint8),
        ("idVendor", ctypes.c_uint16),
        ("idProduct", ctypes.c_uint16),
        ("bcdDevice", ctypes.c_uint16),
        ("iManufacturer", ctypes.c_uint8),
        ("iProduct", ctypes.c_uint8),
        ("iSerialNumber", ctypes.c_uint8),
        ("bNumConfigurations", ctypes.c_uint8),
    ]


class CtypesLibUsbBackend:
    """Minimal ctypes wrapper for the libusb-1.0-compatible FreeBSD base API."""

    def __init__(self, library: object | None = None) -> None:
        if library is None:
            if not sys.platform.startswith("freebsd"):
                raise OSError("native libusb detection is supported only on FreeBSD")
            library = ctypes.CDLL(self._find_library())
            self._configure_signatures(library)
        self._library = library

    @staticmethod
    def _find_library() -> str:
        library = ctypes.util.find_library("usb")
        if library is None:
            raise OSError("FreeBSD native libusb library was not found")
        return library

    @staticmethod
    def _configure_signatures(library: object) -> None:
        device_pointer = ctypes.c_void_p
        device_list_pointer = ctypes.POINTER(device_pointer)

        library.libusb_init.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        library.libusb_init.restype = ctypes.c_int
        library.libusb_exit.argtypes = [ctypes.c_void_p]
        library.libusb_exit.restype = None
        library.libusb_get_device_list.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(device_list_pointer),
        ]
        library.libusb_get_device_list.restype = ctypes.c_ssize_t
        library.libusb_free_device_list.argtypes = [device_list_pointer, ctypes.c_int]
        library.libusb_free_device_list.restype = None
        library.libusb_get_device_descriptor.argtypes = [
            device_pointer,
            ctypes.POINTER(_LibUsbDeviceDescriptor),
        ]
        library.libusb_get_device_descriptor.restype = ctypes.c_int
        library.libusb_get_bus_number.argtypes = [device_pointer]
        library.libusb_get_bus_number.restype = ctypes.c_uint8
        library.libusb_get_device_address.argtypes = [device_pointer]
        library.libusb_get_device_address.restype = ctypes.c_uint8
        library.libusb_open.argtypes = [device_pointer, ctypes.POINTER(ctypes.c_void_p)]
        library.libusb_open.restype = ctypes.c_int
        library.libusb_close.argtypes = [ctypes.c_void_p]
        library.libusb_close.restype = None

    def probe(self, vendor_id: int, product_id: int) -> DetectionResult:
        context = ctypes.c_void_p()
        init_code = self._library.libusb_init(ctypes.byref(context))
        if init_code != LIBUSB_SUCCESS:
            return self._error(init_code, "libusb initialization failed")

        device_list = ctypes.POINTER(ctypes.c_void_p)()
        try:
            count = self._library.libusb_get_device_list(
                context, ctypes.byref(device_list)
            )
            if count < 0:
                return self._error(count, "USB device enumeration failed")

            try:
                return self._probe_device_list(
                    device_list, count, vendor_id, product_id
                )
            finally:
                self._library.libusb_free_device_list(device_list, 1)
        finally:
            self._library.libusb_exit(context)

    def _probe_device_list(
        self,
        device_list: ctypes.POINTER(ctypes.c_void_p),
        count: int,
        vendor_id: int,
        product_id: int,
    ) -> DetectionResult:
        descriptor_error: int | None = None

        for index in range(count):
            device = device_list[index]
            native_descriptor = _LibUsbDeviceDescriptor()
            code = self._library.libusb_get_device_descriptor(
                device, ctypes.byref(native_descriptor)
            )
            if code != LIBUSB_SUCCESS:
                descriptor_error = code
                continue
            if (
                native_descriptor.idVendor != vendor_id
                or native_descriptor.idProduct != product_id
            ):
                continue

            descriptor = self._copy_descriptor(device, native_descriptor)
            handle = ctypes.c_void_p()
            open_code = self._library.libusb_open(device, ctypes.byref(handle))
            if open_code == LIBUSB_ERROR_ACCESS:
                return DetectionResult(
                    status=DetectionStatus.PERMISSION_DENIED,
                    descriptor=descriptor,
                    error_code=open_code,
                    detail="matching DS9490 is visible but cannot be opened",
                )
            if open_code != LIBUSB_SUCCESS:
                return self._error(
                    open_code,
                    "matching DS9490 could not be opened",
                    descriptor,
                )

            self._library.libusb_close(handle)
            return DetectionResult(
                status=DetectionStatus.OPENED,
                descriptor=descriptor,
                detail="matching DS9490 opened and closed successfully",
            )

        if descriptor_error is not None:
            return self._error(
                descriptor_error,
                "one or more USB device descriptors could not be inspected",
            )
        return DetectionResult(
            status=DetectionStatus.ABSENT,
            detail="no USB device matched 04fa:2490",
        )

    def _copy_descriptor(
        self, device: ctypes.c_void_p, native: _LibUsbDeviceDescriptor
    ) -> UsbDeviceDescriptor:
        return UsbDeviceDescriptor(
            vendor_id=native.idVendor,
            product_id=native.idProduct,
            usb_version=native.bcdUSB,
            device_version=native.bcdDevice,
            device_class=native.bDeviceClass,
            device_subclass=native.bDeviceSubClass,
            device_protocol=native.bDeviceProtocol,
            max_packet_size0=native.bMaxPacketSize0,
            num_configurations=native.bNumConfigurations,
            bus_number=self._library.libusb_get_bus_number(device),
            device_address=self._library.libusb_get_device_address(device),
        )

    @staticmethod
    def _error(
        code: int,
        detail: str,
        descriptor: UsbDeviceDescriptor | None = None,
    ) -> DetectionResult:
        return DetectionResult(
            status=DetectionStatus.UNEXPECTED_ERROR,
            descriptor=descriptor,
            error_code=code,
            detail=detail,
        )


def detect_ds9490(backend: DetectorBackend | None = None) -> DetectionResult:
    """Detect and access-test only the Dallas/Maxim DS9490 adapter."""

    if backend is None:
        try:
            backend = CtypesLibUsbBackend()
        except OSError as error:
            return DetectionResult(
                status=DetectionStatus.UNEXPECTED_ERROR,
                detail=str(error),
            )
    return backend.probe(DS9490_VENDOR_ID, DS9490_PRODUCT_ID)

