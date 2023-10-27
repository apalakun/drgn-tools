# Copyright (c) 2023, Oracle and/or its affiliates.
# Licensed under the Universal Permissive License v 1.0 as shown at https://oss.oracle.com/licenses/upl/
"""
Manager for test vmcores - downloaded from OCI block storage
"""
import argparse
import os
import signal
import subprocess
import sys
from concurrent.futures import as_completed
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Any
from typing import List
from typing import Tuple

import oci.config
from oci.exceptions import ConfigFileNotFound
from oci.object_storage import ObjectStorageClient
from oci.object_storage import UploadManager
from oci.pagination import list_call_get_all_results_generator
from rich.progress import BarColumn
from rich.progress import DownloadColumn
from rich.progress import Progress
from rich.progress import TaskID
from rich.progress import TextColumn
from rich.progress import TimeRemainingColumn
from rich.progress import TransferSpeedColumn

from testing.util import gitlab_section

CORE_DIR = Path.cwd() / "testdata/vmcores"

CHUNK_SIZE = 16 * 4096
UPLOAD_PART_SIZE = 16 * 1024 * 1024

SIGTERM_EVENT = Event()
signal.signal(signal.SIGTERM, lambda: SIGTERM_EVENT.set())  # type: ignore


def get_oci_bucket_info() -> Tuple[str, str, str]:
    namespace = os.environ.get("VMCORE_NAMESPACE")
    bucket = os.environ.get("VMCORE_BUCKET")
    prefix = os.environ.get("VMCORE_PREFIX")
    if not (namespace and bucket and prefix):
        raise Exception(
            "Please set VMCORE_NAMESPACE, VMCORE_BUCKET, and VMCORE_PREFIX to "
            "point to the OCI object storage location for the vmcore repo."
        )
    return namespace, bucket, prefix


def download_file(
    client: ObjectStorageClient,
    progress: Progress,
    name: str,
    key: str,
    path: Path,
    size: int,
):
    progress.print(f"Downloading {name}")
    task_id = progress.add_task(
        "download",
        filename=name,
        total=size,
        start=True,
    )
    namespace, bucket, _ = get_oci_bucket_info()
    response = client.get_object(namespace, bucket, key)
    relpath = path.relative_to(CORE_DIR)
    with path.open("wb") as f:
        for content_bytes in response.data.iter_content(chunk_size=CHUNK_SIZE):
            f.write(content_bytes)
            progress.update(task_id, advance=len(content_bytes))
            if SIGTERM_EVENT.is_set():
                progress.print(f"[red]Download interrupted[/red]: {relpath}")
                return
    progress.print(f"Download completed: {relpath}")
    progress.remove_task(task_id)


def all_objects(client: ObjectStorageClient) -> List[Any]:
    objects = []
    namespace, bucket, prefix = get_oci_bucket_info()
    gen = list_call_get_all_results_generator(
        client.list_objects,
        "response",
        namespace,
        bucket,
        prefix=prefix,
        fields="size",
    )
    for response in gen:
        objects.extend(response.data.objects)
    return objects


def download_all(client: ObjectStorageClient):
    _, _, prefix = get_oci_bucket_info()
    progress = Progress(
        TextColumn("[bold blue]{task.fields[filename]}", justify="right"),
        BarColumn(bar_width=None),
        "[progress.percentage]{task.percentage:>3.1f}%",
        "•",
        DownloadColumn(),
        "•",
        TransferSpeedColumn(),
        "•",
        TimeRemainingColumn(),
    )
    objects = all_objects(client)
    CORE_DIR.mkdir(exist_ok=True)
    with progress, ThreadPoolExecutor(max_workers=8) as pool:
        futures = []
        for obj in objects:
            assert obj.name.startswith(prefix)
            name = obj.name[len(prefix) :]
            path = CORE_DIR / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_file() and path.stat().st_size == obj.size:
                progress.print(f"Already exists: {name}")
            else:
                futures.append(
                    pool.submit(
                        download_file,
                        client,
                        progress,
                        name,
                        obj.name,
                        path,
                        obj.size,
                    )
                )
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                print(e)


def delete_orphans(client: ObjectStorageClient):
    print("Searching for orphaned files to remove...")
    objs = all_objects(client)
    keys = set()
    _, _, prefix = get_oci_bucket_info()
    for obj in objs:
        assert obj.name.startswith(prefix)
        name = obj.name[len(prefix) :]
        keys.add(name)

    # Iterate using list() because modifying the directory while iterating it
    # can lead to errors
    for fn in list(CORE_DIR.glob("**/*")):
        if not fn.is_file():
            continue
        key = str(fn.relative_to(CORE_DIR))
        if key in keys:
            continue
        print(f"Remove orphaned file: {key}")
        fn.unlink()
        parent = fn.parent
        while not list(parent.iterdir()):
            print(f"Remove empty parent: {parent}")
            parent.rmdir()
            parent = parent.parent


def upload_file(
    client: ObjectStorageClient,
    progress: Progress,
    task_id: TaskID,
    key: str,
    path: Path,
) -> None:
    def cb(nbytes: int) -> None:
        progress.update(task_id, advance=nbytes)

    namespace, bucket, _ = get_oci_bucket_info()
    progress.start_task(task_id)
    manager = UploadManager(client)
    manager.upload_file(
        namespace,
        bucket,
        key,
        str(path),
        progress_callback=cb,
    )


def upload_all(client: ObjectStorageClient, core: str) -> None:
    _, _, prefix = get_oci_bucket_info()
    progress = Progress(
        TextColumn("[bold blue]{task.fields[filename]}", justify="right"),
        BarColumn(bar_width=None),
        "[progress.percentage]{task.percentage:>3.1f}%",
        "•",
        DownloadColumn(),
        "•",
        TransferSpeedColumn(),
        "•",
        TimeRemainingColumn(),
    )
    core_path = CORE_DIR / core
    vmlinux_path = core_path / "vmlinux"
    vmcore_path = core_path / "vmcore"
    if not vmlinux_path.exists() or not vmcore_path.exists():
        sys.exit("error: missing vmcore or vmlinux file")
    uploads = [vmlinux_path, vmcore_path] + list(core_path.glob("*.ko.debug"))
    object_to_size = {obj.name: obj.size for obj in all_objects(client)}
    with progress, ThreadPoolExecutor(max_workers=4) as pool:
        futures = []
        for path in uploads:
            key = prefix + str(path.relative_to(CORE_DIR))
            existing_size = object_to_size.get(key)
            size = path.stat().st_size
            if existing_size is not None and existing_size == size:
                progress.print(f"Already uploaded: {key}")
                continue
            task_id = progress.add_task(
                "upload",
                filename=key,
                total=path.stat().st_size,
                start=False,
            )
            fut = pool.submit(
                upload_file,
                client,
                progress,
                task_id,
                key,
                path,
            )
            futures.append(fut)
        for future in as_completed(futures):
            future.result()


def test() -> None:
    for path in CORE_DIR.iterdir():
        core_name = path.name
        with gitlab_section(
            f"vmcore-{core_name}",
            f"Running tests on vmcore {core_name}",
            collapsed=True,
        ):
            subprocess.run(
                [
                    "tox",
                    "--",
                    "--vmcore",
                    core_name,
                    "--vmcore-dir",
                    str(CORE_DIR),
                ],
                check=True,
            )


def get_client() -> ObjectStorageClient:
    try:
        config = oci.config.from_file()
        return ObjectStorageClient(config)
    except ConfigFileNotFound:
        sys.exit(
            "error: You need to configure OCI!\n"
            'Try running ".tox/runner/bin/oci setup bootstrap"'
        )


def main():
    global CORE_DIR
    parser = argparse.ArgumentParser(
        description="manages drgn-tools vmcores",
    )
    parser.add_argument(
        "action",
        choices=["download", "upload", "test"],
        help="choose which operation",
    )
    parser.add_argument(
        "--upload-core",
        type=str,
        help="choose name of the vmcore to upload",
    )
    parser.add_argument(
        "--core-directory", type=Path, help="where to store vmcores"
    )
    parser.add_argument(
        "--delete-orphan",
        action="store_true",
        help="delete any files which are not listed on block storage",
    )
    args = parser.parse_args()
    if args.core_directory:
        CORE_DIR = args.core_directory.absolute()
    if args.action == "download":
        client = get_client()
        download_all(client)
        if args.delete_orphan:
            delete_orphans(client)
    elif args.action == "upload":
        if not args.upload_core:
            sys.exit("error: --upload-core is required for upload operation")
        upload_all(get_client(), args.upload_core)
    elif args.action == "test":
        test()


if __name__ == "__main__":
    main()