# Copyright (c) 2023, Oracle and/or its affiliates.
# Licensed under the Universal Permissive License v 1.0 as shown at https://oss.oracle.com/licenses/upl/
"""
Helpers for device mapper devices.
"""
import argparse
from typing import Iterable
from typing import Tuple

from drgn import Object
from drgn import Program
from drgn.helpers.linux.list import list_for_each_entry
from drgn.helpers.linux.rbtree import rbtree_inorder_for_each_entry

from drgn_tools.corelens import CorelensModule
from drgn_tools.module import ensure_debuginfo
from drgn_tools.table import print_table
from drgn_tools.util import BitNumberFlags
from drgn_tools.util import kernel_version


def for_each_dm_hash(prog: Program) -> Iterable[Tuple[Object, str]]:
    for head in prog["_name_buckets"]:
        for hc in list_for_each_entry(
            "struct hash_cell", head.address_of_(), "name_list"
        ):
            yield hc.md, hc.name.string_().decode()


def for_each_dm_rbtree(prog: Program) -> Iterable[Tuple[Object, str]]:
    for hc in rbtree_inorder_for_each_entry(
        "struct hash_cell", prog["name_rb_tree"], "name_node"
    ):
        yield hc.md, hc.name.string_().decode()


def for_each_dm(prog: Program) -> Iterable[Tuple[Object, str]]:
    if "_name_buckets" in prog:
        return for_each_dm_hash(prog)
    elif "name_rb_tree" in prog:
        return for_each_dm_rbtree(prog)
    else:
        raise NotImplementedError("Cannot find dm devices")


class DmFlagsBits(BitNumberFlags):
    """
    Class to convert preprocessor definitions to enum

    drgn can't get the value of preprocessor definitions.
    This is only appliable to the kernel starting 8ae126660fdd
    which was merged by v4.10
    """

    BLOCK_IO_FOR_SUSPEND = 0
    SUSPENDED = 1
    FROZEN = 2
    FREEING = 3
    DELETING = 4
    NOFLUSH_SUSPENDING = 5
    DEFERRED_REMOVE = 6
    SUSPENDED_INTERNALLY = 7
    POST_SUSPENDING = 8
    EMULATE_ZONE_APPEND = 9


class DmFlagsBitsOld(BitNumberFlags):
    """only appliable to kernel older than v4.10"""

    BLOCK_IO_FOR_SUSPEND = 0
    SUSPENDED = 1
    FROZEN = 2
    FREEING = 3
    DELETING = 4
    NOFLUSH_SUSPENDING = 5
    MERGE_IS_OPTIONAL = 6
    DEFERRED_REMOVE = 7
    SUSPENDED_INTERNALLY = 8


def dm_flags(dm: Object) -> str:
    if kernel_version(dm.prog_) < (4, 10, 0):
        return DmFlagsBitsOld.decode(int(dm.flags))
    else:
        return DmFlagsBits.decode(int(dm.flags))


def show_dm(prog: Program) -> None:
    msg = ensure_debuginfo(prog, ["dm_mod"])
    if msg:
        print(msg)
        return

    output = [["NUMBER", "NAME", "MAPPED_DEVICE", "FLAGS"]]
    for dm, name in for_each_dm(prog):
        output.append(
            [
                dm.disk.disk_name.string_().decode(),
                name,
                hex(dm.value_()),
                dm_flags(dm),
            ]
        )
    print_table(output)


class Dm(CorelensModule):
    """Display info about device mapper devices"""

    name = "dm"
    skip_unless_have_kmod = "dm_mod"

    def run(self, prog: Program, args: argparse.Namespace) -> None:
        show_dm(prog)
