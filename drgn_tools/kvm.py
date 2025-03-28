# Copyright (c) 2024, Oracle and/or its affiliates.
# Licensed under the Universal Permissive License v 1.0 as shown at https://oss.oracle.com/licenses/upl/
import argparse
import enum
from typing import Iterable
from typing import Iterator

from drgn import cast
from drgn import Object
from drgn import Program
from drgn.helpers.linux.list import list_for_each_entry
from drgn.helpers.linux.pid import find_task
from drgn.helpers.linux.sched import task_state_to_char
from drgn.helpers.linux.xarray import xa_for_each

from drgn_tools.corelens import CorelensModule
from drgn_tools.table import print_table
from drgn_tools.util import has_member


class KvmVcpuState(enum.Enum):
    """
    Defined in include/uapi/linux/kvm.h, with prefix KVM_MP_STATE_
    """

    RUNNABLE = 0
    UNINITIALIZED = 1
    INIT_RECEIVED = 2
    HALTED = 3
    SIPI_RECEIVED = 4
    STOPPED = 5
    CHECK_STOP = 6
    OPERATING = 7
    LOAD = 8


class MemSlotFlag(enum.Enum):
    """
    kvm_memory_region::flags
    """

    KVM_MEM_LOG_DIRTY_PAGES = 1
    KVM_MEM_READONLY = 2


def for_each_vm(prog: Program) -> Iterable[Object]:
    """
    Iterates over all ``struct kvm *
    """
    try:
        yield from list_for_each_entry(
            "struct kvm", prog["vm_list"].address_of_(), "vm_list"
        )
    except KeyError:
        print("No VMs running")


def for_each_vcpu(vm: Object) -> Iterator[Object]:
    if has_member(vm, "vcpus"):
        vcpu_iterator = iter(vm.vcpus)
    else:
        vcpu_iterator = (
            cast("struct kvm_vcpu *", e)
            for _, e in xa_for_each(vm.vcpu_array.address_of_())
        )
    for struct_vcpu in vcpu_iterator:
        if struct_vcpu.value_() == 0:
            break
        yield struct_vcpu


def print_vm_list(prog: Program) -> None:
    """
    Print information for all the VMs
    """
    vm_list = for_each_vm(prog)

    print(" =============<< VM LIST >>=================")
    print("\n")
    rows = [
        [
            "KVM",
            "ONL",
            "CRT",
            "MEMSLOT_0",
            "MEMSLOT_1",
            "VCPUS",
            "KVM_ARCH",
            "KVM_STAT",
            "PID",
            "TASK",
            "CPU",
            "ST",
        ]
    ]

    for vm in vm_list:
        kvm_addr = hex(vm.value_())
        pid = vm.userspace_pid.value_()
        task = find_task(prog, pid)
        if has_member(task, "cpu"):
            cpu = task.cpu.value_()
        else:
            cpu = task.recent_used_cpu.value_()

        if has_member(vm, "vcpus"):
            vcpu = hex(vm.vcpus.address_of_())
        else:
            vcpu = hex(vm.vcpu_array.address_of_())

        rows.append(
            [
                kvm_addr,
                vm.online_vcpus.counter.value_(),
                vm.created_vcpus.value_(),
                hex(vm.memslots[0].value_()),
                hex(vm.memslots[1].value_()),
                vcpu,
                hex(vm.arch.address_of_()),
                hex(vm.stat.address_of_()),
                pid,
                hex(task.value_()),
                cpu,
                task_state_to_char(task),
            ]
        )
    print_table(rows)


def print_vcpu_list(prog: Program) -> None:
    """
    Print information for all the vcpus of each VM
    """
    vm_list = for_each_vm(prog)

    print(" =============<< VCPU LIST >>=================")
    print("\n")
    rows = [
        [
            "KVM",
            "VCPU",
            "ID",
            "IDX",
            "ARCH",
            "STAT",
            "STAT_ID",
            "STATE",
            "CPU",
            "TASK",
        ]
    ]

    for vm in vm_list:
        for vcpu in for_each_vcpu(vm):
            id = vcpu.vcpu_id.value_()

            if has_member(vcpu, "vcpu_idx"):
                idx = vcpu.vcpu_idx.value_()
            else:
                # The UEK5 does not have kvm_vcpu.vcpu_idx structure member. Print None instead.
                idx = None
            arch = hex(vcpu.arch.address_of_())
            stat = hex(vcpu.stat.address_of_())
            if has_member(vcpu, "stats_id"):
                stat_id = vcpu.stats_id.string_().decode("utf-8")
            else:
                # The UEK5 does not have kvm_vcpu.stats_id structure member. Print None instead.
                stat_id = None
            state = vcpu.arch.mp_state.value_()
            cpu = vcpu.cpu.value_()
            if has_member(vcpu, "wait"):
                task = hex(vcpu.wait.task.value_())
            else:
                # The UEK5 does not have kvm_vcpu.wait structure member. Print None instead.
                task = None
            rows.append(
                [
                    hex(vm.value_()),
                    hex(vcpu.value_()),
                    id,
                    idx,
                    arch,
                    stat,
                    stat_id,
                    KvmVcpuState(state).name,
                    cpu,
                    str(task),
                ]
            )

    print_table(rows)


def print_memslot_info(prog: Program) -> None:
    """
    Print memslots info of VM
    """
    vm_list = for_each_vm(prog)

    print(" =============<< MEMSLOT INFO >>=================\n")

    for vm in vm_list:
        rows = [
            [
                "KVM",
                "KVM_MEMSLOTS",
                "KVM_MEMORY_SLOT",
                "BASE_GFN",
                "PAGES",
                "ARCH",
                "USER_ADDR",
                "FLAGS",
            ]
        ]
        nr_pages = 0
        for memslot in vm.memslots:
            # for UEK5 to UEK7-U2
            if has_member(memslot, "memslots"):
                for j in range(memslot.used_slots.value_()):
                    mm = memslot.memslots[j]
                    gfn = mm.base_gfn.value_()
                    pages = mm.npages.value_()
                    arch = hex(mm.arch.address_of_())
                    usr_addr = hex(mm.userspace_addr.value_())
                    if mm.flags.value_() == 0:
                        flags = mm.flags.value_()
                    else:
                        flags = MemSlotFlag(mm.flags.value_()).name
                    nr_pages = nr_pages + pages
                    rows.append(
                        [
                            hex(vm.value_()),
                            hex(memslot.address_of_()),
                            hex(mm.address_of_()),
                            gfn,
                            pages,
                            arch,
                            usr_addr,
                            flags,
                        ]
                    )
            else:
                # Starting from UEK7-U3
                for vcpu in for_each_vcpu(vm):
                    mmslot = vcpu.last_used_slot
                    gfn = mmslot.base_gfn.value_()
                    pages = mmslot.npages.value_()
                    arch = hex(mmslot.arch.address_of_())
                    usr_addr = hex(mmslot.userspace_addr.value_())
                    if mmslot.flags.value_() == 0:
                        flags = mmslot.flags.value_()
                    else:
                        flags = MemSlotFlag(mmslot.flags.value_()).name
                    nr_pages = nr_pages + pages
                    rows.append(
                        [
                            hex(vm.value_()),
                            hex(memslot.value_()),
                            hex(mmslot.address_of_()),
                            gfn,
                            pages,
                            arch,
                            usr_addr,
                            flags,
                        ]
                    )
        print_table(rows)
        print("\n## Total Pages: %d ##" % (nr_pages))


class KvmUtil(CorelensModule):
    """
    Show all the VM related info from KVM host side
    """

    name = "kvm"

    default_args = [
        [
            "--vms",
            "--vcpu",
            "--mmslot",
        ]
    ]

    def add_args(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--vms",
            dest="list_vm",
            action="store_true",
            help="show all VM info",
        )
        parser.add_argument(
            "--vcpu",
            dest="vcpu_list",
            action="store_true",
            help="show all vcpu info",
        )
        parser.add_argument(
            "--mmslot",
            dest="memslot",
            action="store_true",
            help="show all memslot info",
        )

    def run(self, prog: Program, args: argparse.Namespace) -> None:
        if args.list_vm:
            print_vm_list(prog)
        if args.vcpu_list:
            print_vcpu_list(prog)
        if args.memslot:
            print_memslot_info(prog)
        return
