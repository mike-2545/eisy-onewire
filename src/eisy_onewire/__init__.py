"""DS9490 support for the eisy-onewire plugin."""

from .usb_detector import (
    DS9490_PRODUCT_ID,
    DS9490_VENDOR_ID,
    DetectionResult,
    DetectionStatus,
    UsbDeviceDescriptor,
    UsbDeviceSelector,
    detect_ds9490,
    enumerate_ds9490,
)

__all__ = [
    "DS9490_PRODUCT_ID",
    "DS9490_VENDOR_ID",
    "DetectionResult",
    "DetectionStatus",
    "UsbDeviceDescriptor",
    "UsbDeviceSelector",
    "detect_ds9490",
    "enumerate_ds9490",
]

