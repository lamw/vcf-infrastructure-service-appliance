#!/usr/bin/env python3

import argparse
import subprocess
import sys
import xml.parsers.expat
from xml.dom.minidom import parseString

ovfenv_cmd="/usr/bin/vmtoolsd --cmd 'info-get guestinfo.ovfEnv'"


def debug(s):
    sys.stderr.write(s + " \n")  # Syserr only get logged on the console logs
    sys.stderr.flush()


def get_ovf_properties():
    """
        Return a dict of OVF properties in the ovfenv
    """
    properties = {}
    xml_parts = subprocess.Popen(ovfenv_cmd, shell=True,
                                 stdout=subprocess.PIPE).stdout.read()
    try:
        raw_data = parseString(xml_parts)
    except xml.parsers.expat.ExpatError as err:
        debug(str(err))
        sys.exit(1)
    for property in raw_data.getElementsByTagName('Property'):
        key, value = [ property.attributes['oe:key'].value,
                       property.attributes['oe:value'].value ]
        properties[key] = value
    return properties


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("property_name")
    parser.add_argument("--optional", action="store_true",
                        help="Return an empty value if the property is absent")
    args = parser.parse_args(argv)

    ovf = get_ovf_properties()

    if not args.optional and args.property_name not in ovf:
        debug(f"ovfProperty not found: {args.property_name!r}")
        sys.exit(1)

    print(ovf.get(args.property_name, ""), end="")
    sys.exit(0)


if __name__ == "__main__":
   main(sys.argv[1:])
