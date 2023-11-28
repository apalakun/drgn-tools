# Copyright (c) 2023, Oracle and/or its affiliates.
# Licensed under the Universal Permissive License v 1.0 as shown at https://oss.oracle.com/licenses/upl/
import argparse

from drgn import container_of
from drgn import Program
from drgn.helpers.common import escape_ascii_string
from drgn.helpers.linux.cpumask import for_each_online_cpu
from drgn.helpers.linux.list import list_for_each_entry
from drgn.helpers.linux.percpu import per_cpu
from drgn.helpers.linux.pid import find_task

from drgn_tools.corelens import CorelensModule

# List runqueus per cpu


def run_queue(prog: Program) -> None:
    """
    Process that are in RT and CFS.

    :param prog: drgn program
    :returns: None
    """

    print("Run QUEUE")
    # _cpu = drgn.helpers.linux.cpumask.for_each_online_cpu(prog)
    for cpus in for_each_online_cpu(prog):
        print("\n")
        comm = per_cpu(prog["runqueues"], cpus).curr.comm
        pid = per_cpu(prog["runqueues"], cpus).curr.pid
        runqueue = per_cpu(prog["runqueues"], cpus)
        task = find_task(prog, pid)
        prio_array = hex(
            per_cpu(prog["runqueues"], cpus)
            .rt.active.queue[0]
            .address_of_()
            .value_()
            - 16
        )
        cfs_root = hex(
            per_cpu(prog["runqueues"], cpus)
            .cfs.tasks_timeline.address_of_()
            .value_()
        )
        print(" CPU : ", cpus, "  RUNQUEUE:", hex(runqueue.address_of_()))
        print(
            " \t CURRENT:   PID: ",
            pid.value_(),
            "  TASK:",
            hex(task.value_()),
            "  COMMAND",
            escape_ascii_string(comm.string_()),
        )
        # RT PRIO_ARRAY
        count = 0
        print("\n \t RT PRIO_ARRAY:", prio_array)
        rt_prio_array = runqueue.rt.active.queue
        for que in rt_prio_array:
            for t in list_for_each_entry(
                "struct sched_rt_entity", que.address_of_(), "run_list"
            ):
                count += 1
                tsk = container_of(t, "struct task_struct", "rt")
                print(
                    "[{}]\tPID: {}\tTASK: {}\tCOMMAND: {}".format(
                        tsk.prio.value_(),
                        tsk.pid.value_(),
                        hex(tsk),
                        escape_ascii_string(tsk.comm.string_()),
                    )
                ) if tsk.pid.value_() != 0 else print()
        if count == 0:
            print(" \t\t[no tasks queued]")
        # CFS RB_ROOT
        print("\n \t CFS RB_ROOT:", cfs_root)
        count = 0
        runq = per_cpu(prog["runqueues"], cpus).address_of_()
        for t in list_for_each_entry(
            "struct task_struct", runq.cfs_tasks.address_of_(), "se.group_node"
        ):
            count += 1
            print(
                "[",
                t.prio.value_(),
                "]",
                " PID:",
                t.pid.value_(),
                "\t TASK:",
                hex(t),
                "\tCOMMAND:",
                t.comm.string_().decode("utf-8"),
            )
        if count == 0:
            print(" \t\t[no tasks queued]")


class RunQueue(CorelensModule):
    """
    List process that are in RT and CFS queue.
    """

    name = "runq"

    def run(self, prog: Program, args: argparse.Namespace) -> None:
        run_queue(prog)
