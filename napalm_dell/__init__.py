"""napalm_dell package."""

# Import stdlib
import pkg_resources

# Import local modules
from napalm_dell.dell import DNOS6Driver

try:
    __version__ = pkg_resources.get_distribution('napalm-dell').version
except pkg_resources.DistributionNotFound:
    __version__ = "Not installed"

__all__ = ('DNOS6Driver', )

