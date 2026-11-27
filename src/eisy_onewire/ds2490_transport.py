"""Minimal DS2490 USB transport for a reviewed Search ROM operation.

This module is intentionally not invoked by the plugin entry point.  It defines
the transport for a later, separately approved hardware test.  The native
boundary is injectable so every transaction can be tested without USB hardware.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Callable, Protocol

from .usb_detector import (
    CtypesLibUsbBackend,
    DS9490_PRODUCT_ID,
    DS9490_VENDOR_ID,
    LIBUSB_ERROR_ACCESS,
    LIBUSB_SUCCESS,
    UsbDeviceDescriptor,
    UsbDeviceSelector,
    _LibUsbDeviceDescriptor,
)

LIBUSB_ERROR_NO_DEVICE = -4
LIBUSB_ERROR_NOT_FOUND = -5
LIBUSB_ERROR_BUSY = -6
LIBUSB_ERROR_TIMEOUT = -7
LIBUSB_ERROR_OVERFLOW = -8
LIBUSB_ERROR_PIPE = -9
LIBUSB_ERROR_OTHER = -99

CONFIGURATION = 1
INTERFACE = 0
ALTERNATE_SETTING = 1
EP_STATUS = 0x81
EP_DATA_OUT = 0x02
EP_DATA_IN = 0x83

VENDOR_REQUEST_OUT = 0x40
CONTROL_COMMAND = 0x00
COMMUNICATION_COMMAND = 0x01
MODE_COMMAND = 0x02

CONTROL_RESET_DEVICE = 0x0000
MODE_1WIRE_SPEED = 0x0002
MODE_PULLDOWN_SLEW_RATE = 0x0004
MODE_WRITE1_LOW_TIME = 0x0006
MODE_DATA_SAMPLE_OFFSET = 0x0007
ONEWIRE_SPEED_FLEXIBLE = 0x01
FLEXIBLE_SLEW_RATE = 0x04
FLEXIBLE_WRITE1_LOW_TIME = 0x04
FLEXIBLE_DATA_SAMPLE_OFFSET = 0x04
COMM_SEARCH_ACCESS = 0x00F4
COMM_IMMEDIATE = 0x0001
COMM_SEARCH_MODE = 0x0008
COMM_RESET = 0x0100
COMM_FLUSH_ON_ERROR = 0x0800
COMM_RETURN_DISCREPANCY = 0x4000
SEARCH_ROM = 0xF0

STATUS_SIZE = 32
STATUS_MINIMUM_SIZE = 16
STATUS_SPEED_OFFSET = 1
STATUS_SLEW_RATE_OFFSET = 4
STATUS_WRITE1_LOW_TIME_OFFSET = 5
STATUS_DATA_SAMPLE_OFFSET = 6
STATUS_FLAGS_OFFSET = 8
STATUS_DATA_IN_COUNT_OFFSET = 13
STATUS_IDLE = 0x20
STATUS_HALTED = 0x10
STATUS_ENDPOINT_OVERFLOW = 0x80
RESULT_OFFSET = 16
RESULT_NO_RESPONSE = 0x01
RESULT_SHORT = 0x02
RESULT_END_OF_SEARCH = 0x80
RESULT_DEVICE_DETECT = 0xA5

DEFAULT_TIMEOUT_MS = 1000
DEFAULT_STATUS_POLLS = 100
MAX_SEARCH_DEVICES = 15  # 15 ROMs fit in the DS2490's 128-byte EP3 FIFO.
MAX_DATA_IN_BYTES = 128


class Ds2490Error(RuntimeError):
    """Base class for DS2490 transport failures."""


class AdapterNotFoundError(Ds2490Error):
    """No 04fa:2490 adapter was enumerated."""


class AdapterSelectionAmbiguousError(Ds2490Error):
    """More than one DS9490 matched an operation without an explicit selector."""


class UsbPermissionError(Ds2490Error):
    """The runtime identity cannot open or claim the adapter."""


class UsbInterfaceBusyError(Ds2490Error):
    """Another process or driver already owns interface 0."""


class UsbConfigurationError(Ds2490Error):
    """The active USB configuration cannot support the requested operation."""


class UsbEndpointUnavailableError(Ds2490Error):
    """The expected endpoint is unavailable in the active interface setting."""


class UsbTransferTimeout(Ds2490Error):
    """A native USB transfer exceeded its bounded timeout."""


class UsbTransportError(Ds2490Error):
    """A native libusb operation failed."""

    def __init__(
        self,
        operation: str,
        code: int,
        *,
        requested_length: int | None = None,
        transferred_length: int | None = None,
        status_reported_length: int | None = None,
    ) -> None:
        context = []
        if status_reported_length is not None:
            context.append(f"EP1 reported {status_reported_length} bytes")
        if requested_length is not None:
            context.append(f"EP3 requested {requested_length} bytes")
        if transferred_length is not None:
            context.append(f"libusb reported {transferred_length} transferred")
        detail = f" ({'; '.join(context)})" if context else ""
        super().__init__(f"{operation} failed with libusb error {code}{detail}")
        self.operation = operation
        self.code = code
        self.requested_length = requested_length
        self.transferred_length = transferred_length
        self.status_reported_length = status_reported_length


@dataclass(frozen=True, slots=True)
class UsbSessionPolicy:
    """USB state changes permitted while opening a claimed session."""

    required_configuration: int = CONFIGURATION
    set_configuration_if_needed: bool = True
    alternate_setting: int | None = ALTERNATE_SETTING


SEARCH_SESSION_POLICY = UsbSessionPolicy()
STATUS_DIAGNOSTIC_POLICY = UsbSessionPolicy(
    set_configuration_if_needed=False,
    alternate_setting=None,
)
SEARCH_DIAGNOSTIC_POLICY = UsbSessionPolicy(
    set_configuration_if_needed=False,
    alternate_setting=None,
)


class Ds2490ProtocolError(Ds2490Error):
    """The adapter returned a malformed or unsafe-to-accept response."""


class SearchLimitExceeded(Ds2490ProtocolError):
    """More ROMs exist than the bounded discovery operation can return."""


@dataclass(frozen=True, slots=True)
class Ds2490Status:
    """The state fields needed to safely sequence a DS2490 command."""

    speed: int
    slew_rate: int
    write1_low_time: int
    data_sample_offset: int
    flags: int
    data_in_count: int
    results: tuple[int, ...]

    @property
    def idle(self) -> bool:
        return bool(self.flags & STATUS_IDLE)

    @property
    def halted(self) -> bool:
        return bool(self.flags & STATUS_HALTED)


class UsbSession(Protocol):
    """Claimed DS2490 interface used by the protocol layer."""

    def control(self, request: int, value: int, index: int) -> None: ...

    def bulk_out(self, payload: bytes) -> None: ...

    def bulk_in(self, length: int) -> bytes: ...

    def interrupt_in(self, length: int) -> bytes: ...


class UsbSessionFactory(Protocol):
    """Factory whose context manager always releases native resources."""

    def open(self) -> AbstractContextManager[UsbSession]: ...


class Ds2490Transport:
    """Issue the smallest DS2490 command set needed for Search ROM."""

    def __init__(
        self,
        session: UsbSession,
        *,
        max_status_polls: int = DEFAULT_STATUS_POLLS,
    ) -> None:
        if max_status_polls < 1:
            raise ValueError("max_status_polls must be positive")
        self._session = session
        self._max_status_polls = max_status_polls

    def initialize(self) -> None:
        """Reset command state and apply the reviewed flexible bus profile."""

        self._session.control(CONTROL_COMMAND, CONTROL_RESET_DEVICE, 0)
        self._wait_until_idle(allow_no_devices=False)
        for mode, value in (
            (MODE_1WIRE_SPEED, ONEWIRE_SPEED_FLEXIBLE),
            (MODE_PULLDOWN_SLEW_RATE, FLEXIBLE_SLEW_RATE),
            (MODE_WRITE1_LOW_TIME, FLEXIBLE_WRITE1_LOW_TIME),
            (MODE_DATA_SAMPLE_OFFSET, FLEXIBLE_DATA_SAMPLE_OFFSET),
        ):
            self._session.control(MODE_COMMAND, mode, value)
        configured = self._wait_until_idle(allow_no_devices=False)
        actual = (
            configured.speed,
            configured.slew_rate,
            configured.write1_low_time,
            configured.data_sample_offset,
        )
        expected = (
            ONEWIRE_SPEED_FLEXIBLE,
            FLEXIBLE_SLEW_RATE,
            FLEXIBLE_WRITE1_LOW_TIME,
            FLEXIBLE_DATA_SAMPLE_OFFSET,
        )
        if actual != expected:
            raise Ds2490ProtocolError(
                "DS2490 did not apply flexible timing profile "
                f"speed/slew/write1/sample={actual!r}; expected {expected!r}"
            )

    def search_roms(self, max_devices: int = MAX_SEARCH_DEVICES) -> tuple[bytes, ...]:
        """Return raw 64-bit ROM responses from one bounded Search Access call."""

        if not 1 <= max_devices <= MAX_SEARCH_DEVICES:
            raise ValueError(f"max_devices must be between 1 and {MAX_SEARCH_DEVICES}")

        # The datasheet requires EP2 to contain the ROM from which searching
        # starts.  All zeroes starts at the beginning of the search tree.
        self._session.bulk_out(bytes(8))
        value = (
            COMM_SEARCH_ACCESS
            | COMM_IMMEDIATE
            | COMM_SEARCH_MODE
            | COMM_RESET
            | COMM_FLUSH_ON_ERROR
            | COMM_RETURN_DISCREPANCY
        )
        index = (max_devices << 8) | SEARCH_ROM
        self._session.control(COMMUNICATION_COMMAND, value, index)

        saw_no_response = False
        final_status: Ds2490Status | None = None
        for _ in range(self._max_status_polls):
            status = self._read_status()
            self._raise_for_status(status)
            saw_no_response |= any(
                result & RESULT_NO_RESPONSE
                for result in status.results
                if result != RESULT_DEVICE_DETECT
            )
            if status.idle:
                final_status = status
                break
            if status.halted:
                raise Ds2490ProtocolError("DS2490 halted during Search ROM")
        else:
            raise UsbTransferTimeout("DS2490 did not become idle after Search ROM")

        # Search Access can emit a short EP3 packet while it is still running.
        # Match OWFS and the DS2490 receive contract: wait for IDLE, then use
        # the stable final FIFO count for one exact, bounded read.
        assert final_status is not None  # the loop exits only through IDLE
        requested = final_status.data_in_count
        payload = b""
        if requested:
            try:
                payload = self._session.bulk_in(requested)
            except UsbTransportError as exc:
                raise UsbTransportError(
                    exc.operation,
                    exc.code,
                    requested_length=exc.requested_length,
                    transferred_length=exc.transferred_length,
                    status_reported_length=requested,
                ) from exc
            if len(payload) != requested:
                raise Ds2490ProtocolError(
                    "DS2490 EP3 bulk IN returned "
                    f"{len(payload)} bytes; final idle status required {requested}"
                )

        if not payload and saw_no_response:
            return ()
        if len(payload) % 8:
            raise Ds2490ProtocolError(
                f"Search ROM returned {len(payload)} bytes, not complete ROM blocks"
            )
        blocks = tuple(bytes(payload[offset : offset + 8]) for offset in range(0, len(payload), 8))
        if len(blocks) > max_devices:
            # With RTS set, the block after the requested ROM count is search
            # discrepancy state, not another device identifier.
            raise SearchLimitExceeded(
                f"more than {max_devices} devices are present; response includes discrepancy data"
            )
        if saw_no_response and blocks:
            raise Ds2490ProtocolError(
                "Search ROM reported a no-response condition after partial data"
            )
        return blocks

    def _wait_until_idle(self, *, allow_no_devices: bool) -> Ds2490Status:
        for _ in range(self._max_status_polls):
            status = self._read_status()
            self._raise_for_status(status, allow_no_devices=allow_no_devices)
            if status.idle:
                return status
            if status.halted:
                raise Ds2490ProtocolError("DS2490 entered the halted state")
        raise UsbTransferTimeout("DS2490 did not become idle")

    def _read_status(self) -> Ds2490Status:
        raw = self._session.interrupt_in(STATUS_SIZE)
        if not STATUS_MINIMUM_SIZE <= len(raw) <= STATUS_SIZE:
            raise Ds2490ProtocolError(
                f"DS2490 status was {len(raw)} bytes; 16..32 are required"
            )
        if raw[STATUS_DATA_IN_COUNT_OFFSET] > MAX_DATA_IN_BYTES:
            raise Ds2490ProtocolError(
                "DS2490 data-in buffer count exceeds its 128-byte FIFO"
            )
        return Ds2490Status(
            speed=raw[STATUS_SPEED_OFFSET],
            slew_rate=raw[STATUS_SLEW_RATE_OFFSET],
            write1_low_time=raw[STATUS_WRITE1_LOW_TIME_OFFSET],
            data_sample_offset=raw[STATUS_DATA_SAMPLE_OFFSET],
            flags=raw[STATUS_FLAGS_OFFSET],
            data_in_count=raw[STATUS_DATA_IN_COUNT_OFFSET],
            results=tuple(raw[RESULT_OFFSET:]),
        )

    @staticmethod
    def _raise_for_status(
        status: Ds2490Status, *, allow_no_devices: bool = True
    ) -> None:
        if status.flags & STATUS_ENDPOINT_OVERFLOW:
            raise Ds2490ProtocolError("DS2490 reported an endpoint FIFO overflow")
        for result in status.results:
            if result == RESULT_DEVICE_DETECT:
                continue
            if result & RESULT_SHORT:
                raise Ds2490ProtocolError("DS2490 reported a short on the 1-Wire bus")
            if result & RESULT_NO_RESPONSE and not allow_no_devices:
                raise Ds2490ProtocolError("DS2490 reported no 1-Wire response")
            # EOS is expected when fewer devices exist than the requested cap.
            unsupported = result & ~(RESULT_NO_RESPONSE | RESULT_END_OF_SEARCH)
            if unsupported:
                raise Ds2490ProtocolError(
                    f"DS2490 returned unexpected result flags 0x{result:02x}"
                )


class Ds2490Adapter:
    """Own a session for one initialization and one discovery transaction."""

    def __init__(self, factory: UsbSessionFactory | None = None) -> None:
        self._factory = factory or CtypesLibUsbSessionFactory.for_search_diagnostic()

    def search_roms(self, max_devices: int = MAX_SEARCH_DEVICES) -> tuple[bytes, ...]:
        with self._factory.open() as session:
            transport = Ds2490Transport(session)
            transport.initialize()
            return transport.search_roms(max_devices)


class _NativeUsbSession(AbstractContextManager["_NativeUsbSession"]):
    def __init__(
        self,
        library: object,
        context: ctypes.c_void_p,
        handle: ctypes.c_void_p,
        *,
        owns_handle: bool = True,
        handle_failed: Callable[[], None] | None = None,
    ) -> None:
        self._library = library
        self._context = context
        self._handle = handle
        self._claimed = True
        self._closed = False
        self._owns_handle = owns_handle
        self._handle_failed = handle_failed

    def __enter__(self) -> "_NativeUsbSession":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close(suppress_release_error=exc_type is not None)

    def close(self, *, suppress_release_error: bool = False) -> None:
        if self._closed:
            return
        release_code = LIBUSB_SUCCESS
        try:
            if self._claimed:
                release_code = self._library.libusb_release_interface(
                    self._handle, INTERFACE
                )
        finally:
            if self._owns_handle:
                try:
                    self._library.libusb_close(self._handle)
                finally:
                    self._library.libusb_exit(self._context)
            self._closed = True
        if release_code < LIBUSB_SUCCESS and self._handle_failed is not None:
            self._handle_failed()
        if release_code != LIBUSB_SUCCESS and not suppress_release_error:
            _check_operation("release DS2490 interface", release_code)

    def control(self, request: int, value: int, index: int) -> None:
        code = self._library.libusb_control_transfer(
            self._handle,
            VENDOR_REQUEST_OUT,
            request,
            value,
            index,
            None,
            0,
            DEFAULT_TIMEOUT_MS,
        )
        self._check_transfer("control transfer", code)

    def bulk_out(self, payload: bytes) -> None:
        buffer = (ctypes.c_uint8 * len(payload)).from_buffer_copy(payload)
        transferred = ctypes.c_int()
        code = self._library.libusb_bulk_transfer(
            self._handle,
            EP_DATA_OUT,
            buffer,
            len(payload),
            ctypes.byref(transferred),
            DEFAULT_TIMEOUT_MS,
        )
        self._check_transfer("bulk OUT transfer", code)
        if transferred.value != len(payload):
            raise Ds2490ProtocolError(
                f"bulk OUT transferred {transferred.value} of {len(payload)} bytes"
            )

    def bulk_in(self, length: int) -> bytes:
        return self._receive(
            "bulk IN transfer",
            self._library.libusb_bulk_transfer,
            EP_DATA_IN,
            length,
        )

    def interrupt_in(self, length: int) -> bytes:
        return self._receive(
            "interrupt IN transfer",
            self._library.libusb_interrupt_transfer,
            EP_STATUS,
            length,
        )

    def _receive(self, operation: str, function: object, endpoint: int, length: int) -> bytes:
        buffer = (ctypes.c_uint8 * length)()
        transferred = ctypes.c_int()
        code = function(
            self._handle,
            endpoint,
            buffer,
            length,
            ctypes.byref(transferred),
            DEFAULT_TIMEOUT_MS,
        )
        if code < LIBUSB_SUCCESS:
            if code == LIBUSB_ERROR_OTHER:
                if self._handle_failed is not None:
                    self._handle_failed()
                raise UsbTransportError(
                    operation,
                    code,
                    requested_length=length,
                    transferred_length=transferred.value,
                )
            self._check_transfer(operation, code)
        if not 0 <= transferred.value <= length:
            raise Ds2490ProtocolError(
                f"{operation} reported invalid length {transferred.value}"
            )
        return bytes(buffer[: transferred.value])

    def _check_transfer(self, operation: str, code: int) -> None:
        if code < LIBUSB_SUCCESS and self._handle_failed is not None:
            self._handle_failed()
        _check_transfer(operation, code)


class CtypesLibUsbSessionFactory:
    """Open and exclusively claim the DS2490 with FreeBSD base libusb."""

    def __init__(
        self,
        library: object | None = None,
        *,
        policy: UsbSessionPolicy = SEARCH_SESSION_POLICY,
        selector: UsbDeviceSelector | None = None,
    ) -> None:
        if library is None:
            if not sys.platform.startswith("freebsd"):
                raise OSError("native DS2490 transport is supported only on FreeBSD")
            name = ctypes.util.find_library("usb")
            if name is None:
                raise OSError("FreeBSD native libusb library was not found")
            library = ctypes.CDLL(name)
            self._configure_signatures(library)
        self._library = library
        self._policy = policy
        self._selector = selector
        self.selected_descriptor: UsbDeviceDescriptor | None = None

    @classmethod
    def for_status_diagnostic(
        cls,
        library: object | None = None,
        *,
        selector: UsbDeviceSelector | None = None,
    ) -> "CtypesLibUsbSessionFactory":
        """Open without changing configuration or alternate setting."""

        return cls(library, policy=STATUS_DIAGNOSTIC_POLICY, selector=selector)

    @classmethod
    def for_search_diagnostic(
        cls,
        library: object | None = None,
        *,
        selector: UsbDeviceSelector | None = None,
    ) -> "CtypesLibUsbSessionFactory":
        """Claim interface 0 without changing configuration or alternate setting."""

        return cls(library, policy=SEARCH_DIAGNOSTIC_POLICY, selector=selector)

    @staticmethod
    def _configure_signatures(library: object) -> None:
        # Detector signatures cover enumeration; configure the complete Phase 5
        # synchronous API here so ctypes never guesses pointer or integer widths.
        from .usb_detector import CtypesLibUsbBackend

        CtypesLibUsbBackend._configure_signatures(library)
        library.libusb_get_configuration.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        library.libusb_get_configuration.restype = ctypes.c_int
        library.libusb_set_configuration.argtypes = [ctypes.c_void_p, ctypes.c_int]
        library.libusb_set_configuration.restype = ctypes.c_int
        library.libusb_claim_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
        library.libusb_claim_interface.restype = ctypes.c_int
        library.libusb_release_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
        library.libusb_release_interface.restype = ctypes.c_int
        library.libusb_set_interface_alt_setting.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
        ]
        library.libusb_set_interface_alt_setting.restype = ctypes.c_int
        library.libusb_control_transfer.argtypes = [
            ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint16,
            ctypes.c_uint16, ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint16,
            ctypes.c_uint,
        ]
        library.libusb_control_transfer.restype = ctypes.c_int
        transfer_args = [
            ctypes.c_void_p, ctypes.c_uint8, ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_uint,
        ]
        library.libusb_bulk_transfer.argtypes = transfer_args
        library.libusb_bulk_transfer.restype = ctypes.c_int
        library.libusb_interrupt_transfer.argtypes = transfer_args
        library.libusb_interrupt_transfer.restype = ctypes.c_int

    def open(self) -> _NativeUsbSession:
        context = ctypes.c_void_p()
        code = self._library.libusb_init(ctypes.byref(context))
        _check_operation("libusb initialization", code)
        handle: ctypes.c_void_p | None = None
        claimed = False
        try:
            handle = self._open_matching(context)
            configuration = ctypes.c_int()
            code = self._library.libusb_get_configuration(handle, ctypes.byref(configuration))
            _check_operation("read USB configuration", code)
            if configuration.value != self._policy.required_configuration:
                if not self._policy.set_configuration_if_needed:
                    raise UsbConfigurationError(
                        "DS2490 status endpoint requires active USB configuration 1; "
                        f"found {configuration.value} and left it unchanged"
                    )
                code = self._library.libusb_set_configuration(
                    handle, self._policy.required_configuration
                )
                _check_operation("set USB configuration", code)
            code = self._library.libusb_claim_interface(handle, INTERFACE)
            _check_operation("claim DS2490 interface", code)
            claimed = True
            if self._policy.alternate_setting is not None:
                code = self._library.libusb_set_interface_alt_setting(
                    handle, INTERFACE, self._policy.alternate_setting
                )
                _check_operation("select DS2490 alternate setting", code)
            return _NativeUsbSession(self._library, context, handle)
        except BaseException:
            if handle is not None:
                if claimed:
                    self._library.libusb_release_interface(handle, INTERFACE)
                self._library.libusb_close(handle)
            self._library.libusb_exit(context)
            raise

    def _open_matching(self, context: ctypes.c_void_p) -> ctypes.c_void_p:
        devices = ctypes.POINTER(ctypes.c_void_p)()
        count = self._library.libusb_get_device_list(context, ctypes.byref(devices))
        _check_operation("enumerate USB devices", count)
        descriptor_error: int | None = None
        matches: list[tuple[ctypes.c_void_p, UsbDeviceDescriptor]] = []
        descriptor_backend = CtypesLibUsbBackend(self._library)
        try:
            for offset in range(count):
                descriptor = _LibUsbDeviceDescriptor()
                code = self._library.libusb_get_device_descriptor(
                    devices[offset], ctypes.byref(descriptor)
                )
                if code != LIBUSB_SUCCESS:
                    descriptor_error = code
                    continue
                if (
                    descriptor.idVendor != DS9490_VENDOR_ID
                    or descriptor.idProduct != DS9490_PRODUCT_ID
                ):
                    continue
                matches.append(
                    (
                        devices[offset],
                        descriptor_backend._copy_descriptor(
                            devices[offset], descriptor
                        ),
                    )
                )
            if descriptor_error is not None:
                raise UsbTransportError(
                    "inspect USB device descriptors", descriptor_error
                )
            candidates = matches
            if self._selector is not None:
                candidates = [
                    item for item in matches if self._selector.matches(item[1])
                ]
                if not candidates:
                    raise AdapterNotFoundError(
                        "the explicitly selected DS9490 is no longer present"
                    )
            elif not candidates:
                raise AdapterNotFoundError("no USB device matched 04fa:2490")
            if len(candidates) != 1:
                raise AdapterSelectionAmbiguousError(
                    f"{len(candidates)} DS9490 adapters matched; "
                    "explicit selection is required"
                )
            device, selected_descriptor = candidates[0]
            handle = ctypes.c_void_p()
            code = self._library.libusb_open(device, ctypes.byref(handle))
            _check_operation("open DS9490", code)
            self.selected_descriptor = selected_descriptor
            return handle
        finally:
            self._library.libusb_free_device_list(devices, 1)


class PersistentCtypesLibUsbSessionFactory:
    """Claim repeated sessions on one explicitly verified, still-open handle.

    The retained handle is the binding.  Enumeration location is deliberately
    not used to reopen a replacement device: after disconnect, this object is
    permanently invalid and a new explicit identity verification is required.
    """

    def __init__(
        self,
        library: object,
        context: ctypes.c_void_p,
        handle: ctypes.c_void_p,
        descriptor: UsbDeviceDescriptor,
        *,
        policy: UsbSessionPolicy = SEARCH_DIAGNOSTIC_POLICY,
    ) -> None:
        self._library = library
        self._context = context
        self._handle = handle
        self._descriptor = descriptor
        self._policy = policy
        self._invalid = False
        self._closed = False
        self._session_active = False

    @property
    def descriptor(self) -> UsbDeviceDescriptor:
        return self._descriptor

    @property
    def valid(self) -> bool:
        return not self._invalid and not self._closed

    @classmethod
    def establish(
        cls,
        library: object | None = None,
        *,
        policy: UsbSessionPolicy = SEARCH_DIAGNOSTIC_POLICY,
    ) -> "PersistentCtypesLibUsbSessionFactory":
        opener = CtypesLibUsbSessionFactory(library, policy=policy)
        context = ctypes.c_void_p()
        code = opener._library.libusb_init(ctypes.byref(context))
        _check_operation("libusb initialization", code)
        handle: ctypes.c_void_p | None = None
        try:
            handle = opener._open_matching(context)
            assert opener.selected_descriptor is not None
            return cls(
                opener._library,
                context,
                handle,
                opener.selected_descriptor,
                policy=policy,
            )
        except BaseException:
            if handle is not None:
                opener._library.libusb_close(handle)
            opener._library.libusb_exit(context)
            raise

    def open(self) -> _NativeUsbSession:
        if not self.valid:
            raise AdapterNotFoundError(
                "verified DS9490 binding is stale; explicitly verify identity again"
            )
        if self._session_active:
            raise UsbInterfaceBusyError("verified DS9490 already has an active session")
        configuration = ctypes.c_int()
        code = self._library.libusb_get_configuration(
            self._handle, ctypes.byref(configuration)
        )
        self._check_bound_operation("read USB configuration", code)
        if configuration.value != self._policy.required_configuration:
            if not self._policy.set_configuration_if_needed:
                raise UsbConfigurationError(
                    "verified DS9490 requires active USB configuration 1; "
                    f"found {configuration.value} and left it unchanged"
                )
            code = self._library.libusb_set_configuration(
                self._handle, self._policy.required_configuration
            )
            self._check_bound_operation("set USB configuration", code)
        code = self._library.libusb_claim_interface(self._handle, INTERFACE)
        self._check_bound_operation("claim DS2490 interface", code)
        try:
            if self._policy.alternate_setting is not None:
                code = self._library.libusb_set_interface_alt_setting(
                    self._handle, INTERFACE, self._policy.alternate_setting
                )
                self._check_bound_operation("select DS2490 alternate setting", code)
        except BaseException:
            self._library.libusb_release_interface(self._handle, INTERFACE)
            raise
        self._session_active = True
        session = _NativeUsbSession(
            self._library,
            self._context,
            self._handle,
            owns_handle=False,
            handle_failed=self.invalidate,
        )
        original_close = session.close

        def close(*, suppress_release_error: bool = False) -> None:
            try:
                original_close(suppress_release_error=suppress_release_error)
            finally:
                self._session_active = False

        session.close = close  # type: ignore[method-assign]
        return session

    def invalidate(self) -> None:
        self._invalid = True

    def close(self) -> None:
        if self._closed:
            return
        if self._session_active:
            raise UsbInterfaceBusyError(
                "cannot close verified DS9490 while a session is active"
            )
        try:
            self._library.libusb_close(self._handle)
        finally:
            self._library.libusb_exit(self._context)
            self._closed = True
            self._invalid = True

    def _check_bound_operation(self, operation: str, code: int) -> None:
        if code < LIBUSB_SUCCESS:
            self.invalidate()
        _check_operation(operation, code)


def _check_transfer(operation: str, code: int) -> None:
    if code == LIBUSB_ERROR_TIMEOUT:
        raise UsbTransferTimeout(f"{operation} timed out")
    if code in (LIBUSB_ERROR_NOT_FOUND, LIBUSB_ERROR_PIPE):
        raise UsbEndpointUnavailableError(
            f"{operation} could not access the expected endpoint "
            f"(libusb error {code})"
        )
    if code == LIBUSB_ERROR_OVERFLOW:
        raise Ds2490ProtocolError(f"{operation} returned more data than requested")
    _check_operation(operation, code)


def _check_operation(operation: str, code: int) -> None:
    if code >= LIBUSB_SUCCESS:
        return
    if code == LIBUSB_ERROR_ACCESS:
        raise UsbPermissionError(f"{operation} was denied (libusb error {code})")
    if code == LIBUSB_ERROR_BUSY:
        raise UsbInterfaceBusyError(
            f"{operation} found interface 0 busy (libusb error {code})"
        )
    if code == LIBUSB_ERROR_NO_DEVICE:
        raise AdapterNotFoundError(f"{operation}: adapter was disconnected")
    if code == LIBUSB_ERROR_TIMEOUT:
        raise UsbTransferTimeout(f"{operation} timed out")
    raise UsbTransportError(operation, code)
