__version__ = "1.4.0"

from .usb_topology import needs_multiplexing, probe_usb_bus_groups
from .multiplex import MultiplexGroup, MultiplexScheduler
from .v4l2_backend import V4L2Camera
