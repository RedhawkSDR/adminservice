import pkg_resources
import sys

def main(out=sys.stdout):
    config = pkg_resources.resource_string(__name__, 'skel/sample.admin')
    out.write(config)

def domain(out=sys.stdout):
    config = pkg_resources.resource_string(__name__, 'skel/sample.domains')
    out.write(config)

def node(out=sys.stdout):
    config = pkg_resources.resource_string(__name__, 'skel/sample.ndoes')
    out.write(config)

def waveform(out=sys.stdout):
    config = pkg_resources.resource_string(__name__, 'skel/sample.waveforms')
    out.write(config)
