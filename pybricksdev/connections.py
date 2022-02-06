# SPDX-License-Identifier: MIT
# Copyright (c) 2021-2022 The Pybricks Authors

import asyncio
import logging
import os
import struct
from typing import Awaitable, TypeVar

import asyncssh
import semver
import rx.operators as op
from bleak import BleakClient
from bleak.backends.device import BLEDevice
from serial.tools import list_ports
from serial import Serial
from tqdm.auto import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm
from rx.subject import Subject, BehaviorSubject, AsyncSubject

from .ble.lwp3.bytecodes import HubKind
from .ble.nus import NUS_RX_UUID, NUS_TX_UUID
from .ble.pybricks import (
    PYBRICKS_CONTROL_UUID,
    PYBRICKS_PROTOCOL_VERSION,
    SW_REV_UUID,
    PNP_ID_UUID,
    Event,
    StatusFlag,
    unpack_pnp_id,
)
from .compile import compile_file
from .tools import chunk
from .tools.checksum import xor_bytes

logger = logging.getLogger(__name__)

T = TypeVar("T")


class EV3Connection:
    """ev3dev SSH connection for running pybricks-micropython scripts.

    This wraps convenience functions around the asyncssh client.
    """

    _HOME = "/home/robot"
    _USER = "robot"
    _PASSWORD = "maker"

    def abs_path(self, path):
        return os.path.join(self._HOME, path)

    async def connect(self, address):
        """Connects to ev3dev using SSH with a known IP address.

        Arguments:
            address (str):
                IP address of the EV3 brick running ev3dev.

        Raises:
            OSError:
                Connect failed.
        """

        print("Connecting to", address, "...", end=" ")
        self.client = await asyncssh.connect(
            address, username=self._USER, password=self._PASSWORD
        )
        print("Connected.", end=" ")
        self.client.sftp = await self.client.start_sftp_client()
        await self.client.sftp.chdir(self._HOME)
        print("Opened SFTP.")

    async def beep(self):
        """Makes the EV3 beep."""
        await self.client.run("beep")

    async def disconnect(self):
        """Closes the connection."""
        self.client.sftp.exit()
        self.client.close()

    async def download(self, local_path):
        """Downloads a file to the EV3 Brick using sftp.

        Arguments:
            local_path (str):
                Path to the file to be downloaded. Relative to current working
                directory. This same tree will be created on the EV3 if it
                does not already exist.
        """
        # Compute paths
        dirs, file_name = os.path.split(local_path)

        # Make sure same directory structure exists on EV3
        if not await self.client.sftp.exists(self.abs_path(dirs)):
            # If not, make the folders one by one
            total = ""
            for name in dirs.split(os.sep):
                total = os.path.join(total, name)
                if not await self.client.sftp.exists(self.abs_path(total)):
                    await self.client.sftp.mkdir(self.abs_path(total))

        # Send script to EV3
        remote_path = self.abs_path(local_path)
        await self.client.sftp.put(local_path, remote_path)
        return remote_path

    async def run(self, local_path, wait=True):
        """Downloads and runs a Pybricks MicroPython script.

        Arguments:
            local_path (str):
                Path to the file to be downloaded. Relative to current working
                directory. This same tree will be created on the EV3 if it
                does not already exist.
            wait (bool):
                Whether to wait for any output until the program completes.
        """

        # Send script to the hub
        remote_path = await self.download(local_path)

        # Run it and return stderr to get Pybricks MicroPython output
        print("Now starting:", remote_path)
        prog = "brickrun -r -- pybricks-micropython {0}".format(remote_path)

        # Run process asynchronously and print output as it comes in
        async with self.client.create_process(prog) as process:
            # Keep going until the process is done
            while process.exit_status is None and wait:
                try:
                    line = await asyncio.wait_for(
                        process.stderr.readline(), timeout=0.1
                    )
                    print(line.strip())
                except asyncio.TimeoutError:
                    pass

    async def get(self, remote_path, local_path=None):
        """Gets a file from the EV3 over sftp.

        Arguments:
            remote_path (str):
                Path to the file to be fetched. Relative to ev3 home directory.
            local_path (str):
                Path to save the file. Defaults to same as remote_path.
        """
        if local_path is None:
            local_path = remote_path
        await self.client.sftp.get(self.abs_path(remote_path), localpath=local_path)


class PybricksHub:
    EOL = b"\r\n"  # MicroPython EOL

    def __init__(self):
        self.disconnect_observable = AsyncSubject()
        self.status_observable = BehaviorSubject(StatusFlag(0))
        self.nus_observable = Subject()
        self.stream_buf = bytearray()
        self.output = []
        self.print_output = True

        # indicates that the hub is currently connected via BLE
        self.connected = False

        # indicates is we are currently downloading a program
        self.loading = False

        self.hub_kind: HubKind
        self.hub_variant: int

        # File handle for logging
        self.log_file = None

    def line_handler(self, line):
        """Handles new incoming lines. Handle special actions if needed,
        otherwise just print it as regular lines.

        Arguments:
            line (bytearray):
                Line to process.
        """

        # The line tells us to open a log file, so do it.
        if b"PB_OF:" in line or b"_file_begin_ " in line:
            if self.log_file is not None:
                raise RuntimeError("Log file is already open!")

            path_start = len(b"PB_OF:") if b"PB_OF:" in line else len(b"_file_begin_ ")

            # Get path relative to running script, so log will go
            # in the same folder unless specified otherwise.
            full_path = os.path.join(self.script_dir, line[path_start:].decode())
            dir_path, _ = os.path.split(full_path)
            if not os.path.exists(dir_path):
                os.makedirs(dir_path)

            logger.info("Saving log to {0}.".format(full_path))
            self.log_file = open(full_path, "w")
            return

        # The line tells us to close a log file, so do it.
        if b"PB_EOF" in line or b"_file_end_" in line:
            if self.log_file is None:
                raise RuntimeError("No log file is currently open!")
            logger.info("Done saving log.")
            self.log_file.close()
            self.log_file = None
            return

        # If we are processing datalog, save current line to the open file.
        if self.log_file is not None:
            print(line.decode(), file=self.log_file)
            return

        # If there is nothing special about this line, print it if requested.
        self.output.append(line)
        if self.print_output:
            print(line.decode())

    def nus_handler(self, sender, data):
        self.nus_observable.on_next(data)

        # Store incoming data
        if not self.loading:
            self.stream_buf += data
            logger.debug("NUS DATA: {0}".format(data))

        # Break up data into lines and take those out of the buffer
        lines = []
        while True:
            # Find and split at end of line
            index = self.stream_buf.find(self.EOL)
            # If no more line end is found, we are done
            if index < 0:
                break
            # If we found a line, save it, and take it from the buffer
            lines.append(self.stream_buf[0:index])
            del self.stream_buf[0 : index + len(self.EOL)]

        # Call handler for each line that we found
        for line in lines:
            self.line_handler(line)

    def pybricks_service_handler(self, _: int, data: bytes) -> None:
        if data[0] == Event.STATUS_REPORT:
            # decode the payload
            (flags,) = struct.unpack_from("<I", data, 1)
            self.status_observable.on_next(StatusFlag(flags))

    async def connect(self, device: BLEDevice):
        """Connects to a device that was discovered with :meth:`pybricksdev.ble.find_device`

        Args:
            device: The device to connect to.

        Raises:
            BleakError: if connecting failed (or old firmware without Device
                Information Service)
            RuntimeError: if Pybricks Protocol version is not supported
        """
        logger.info(f"Connecting to {device.name}")

        def handle_disconnect(client: BleakClient):
            logger.info("Disconnected!")
            self.disconnect_observable.on_next(True)
            self.disconnect_observable.on_completed()
            self.connected = False

        self.client = BleakClient(device, disconnected_callback=handle_disconnect)

        await self.client.connect()

        try:
            logger.info("Connected successfully!")
            protocol_version = await self.client.read_gatt_char(SW_REV_UUID)
            protocol_version = semver.VersionInfo.parse(protocol_version.decode())

            if (
                protocol_version < PYBRICKS_PROTOCOL_VERSION
                or protocol_version >= PYBRICKS_PROTOCOL_VERSION.bump_major()
            ):
                raise RuntimeError(
                    f"Unsupported Pybricks protocol version: {protocol_version}"
                )

            pnp_id = await self.client.read_gatt_char(PNP_ID_UUID)
            _, _, self.hub_kind, self.hub_variant = unpack_pnp_id(pnp_id)

            await self.client.start_notify(NUS_TX_UUID, self.nus_handler)
            await self.client.start_notify(
                PYBRICKS_CONTROL_UUID, self.pybricks_service_handler
            )
            self.connected = True
        except:  # noqa: E722
            self.disconnect()
            raise

    async def disconnect(self):
        if self.connected:
            logger.info("Disconnecting...")
            await self.client.disconnect()
        else:
            logger.debug("already disconnected")

    async def race_disconnect(self, awaitable: Awaitable[T]) -> T:
        """
        Races an awaitable against a disconnect event.

        If a disconnect event occurs before the awaitable is complete, a
        ``RuntimeError`` is raised and the awaitable is canceled.

        Otherwise, the result of the awaitable is returned. If the awaitable
        raises an exception, that exception will be raised.

        Args:
            awaitable: Any awaitable such as a coroutine.

        Returns:
            The result of the awaitable.

        Raises:
            RuntimeError:
                Thrown if the hub is disconnected before the awaitable completed.
        """
        awaitable_task = asyncio.ensure_future(awaitable)

        disconnect_event = asyncio.Event()
        disconnect_task = asyncio.ensure_future(disconnect_event.wait())

        with self.disconnect_observable.subscribe(lambda _: disconnect_event.set()):
            done, pending = await asyncio.wait(
                {awaitable_task, disconnect_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            for t in pending:
                t.cancel()

            if awaitable_task not in done:
                raise RuntimeError("disconnected during operation")

            return awaitable_task.result()

    async def write(self, data, with_response=False):
        await self.client.write_gatt_char(NUS_RX_UUID, bytearray(data), with_response)

    async def run(self, py_path, wait=True, print_output=True):

        # Reset output buffer
        self.log_file = None
        self.output = []
        self.print_output = print_output

        # Compile the script to mpy format
        self.script_dir, _ = os.path.split(py_path)
        mpy = await compile_file(py_path)

        try:
            self.loading = True

            queue: asyncio.Queue[bytes] = asyncio.Queue()
            subscription = self.nus_observable.subscribe(
                lambda data: queue.put_nowait(data)
            )

            async def send_block(data: bytes) -> None:
                """
                In order to prevent sending data to the hub faster than it can
                be processed, it is sent in blocks of 100 bytes or less. Then
                we wait for the hub to send a checksum to acknowledge that it
                has processed the data.

                Args:
                    data: The data to send (100 bytes or less).
                """
                if self.hub_kind == HubKind.BOOST:
                    # BOOST Move hub has fixed MTU of 23 so we can only send 20
                    # bytes at a time
                    for c in chunk(data, 20):
                        await self.client.write_gatt_char(NUS_RX_UUID, c, False)
                else:
                    await self.client.write_gatt_char(NUS_RX_UUID, data, False)

                msg = await asyncio.wait_for(
                    self.race_disconnect(queue.get()), timeout=0.5
                )
                actual_checksum = msg[0]
                expected_checksum = xor_bytes(data, 0)

                if actual_checksum != expected_checksum:
                    raise RuntimeError(
                        f"bad checksum: expecting {hex(expected_checksum)} but received {hex(actual_checksum)}"
                    )

            # Get length of file and send it as bytes to hub
            length = len(mpy).to_bytes(4, byteorder="little")
            await send_block(length)

            # Send the data chunk by chunk
            with logging_redirect_tqdm(), tqdm(
                total=len(mpy), unit="B", unit_scale=True
            ) as pbar:
                for c in chunk(mpy, 100):
                    await send_block(c)
                    pbar.update(len(c))
        finally:
            subscription.dispose()
            self.loading = False

        if wait:
            user_program_running: asyncio.Queue[bool] = asyncio.Queue()

            with self.status_observable.pipe(
                op.map(lambda s: bool(s & StatusFlag.USER_PROGRAM_RUNNING)),
                op.distinct_until_changed(),
            ).subscribe(lambda s: user_program_running.put_nowait(s)):

                # The first item in the queue is the current status. The status
                # could change before or after the last checksum is received,
                # so this could be either true or false.
                is_running = await self.race_disconnect(user_program_running.get())

                if not is_running:
                    # if the program has not already started, wait a short time
                    # for it to start
                    try:
                        await asyncio.wait_for(
                            self.race_disconnect(user_program_running.get()), 0.2
                        )
                    except asyncio.TimeoutError:
                        # if it doesn't start, assume it was a very short lived
                        # program and we just missed the status message
                        return

                # At this point, we know the user program is running, so the
                # next item in the queue must indicate that the user program
                # has stopped.
                is_running = await self.race_disconnect(user_program_running.get())

                # maybe catch mistake if the code is changed
                assert not is_running

                # sleep is a hack to receive all output from user program since
                # the firmware currently doesn't flush the buffer before clearing
                # the user program running status flag
                # https://github.com/pybricks/support/issues/305
                await asyncio.sleep(0.3)


FILE_PACKET_SIZE = 1024
FILE_TRANSFER_SCRIPT = f"""
import sys
import micropython
import utime

PACKETSIZE = {FILE_PACKET_SIZE}

def receive_file(filename, filesize):

    micropython.kbd_intr(-1)

    with open(filename, "wb") as f:

        # Initialize buffers
        done = 0
        buf = bytearray(PACKETSIZE)
        sys.stdin.buffer.read(1)

        while done < filesize:

            # Size of last package
            if filesize - done < PACKETSIZE:
                buf = bytearray(filesize - done)

            # Read one packet from standard in.
            time_now = utime.ticks_ms()
            bytes_read = sys.stdin.buffer.readinto(buf)

            # If transmission took a long time, something bad happened.
            if utime.ticks_ms() - time_now > 5000:
                print("transfer timed out")
                return

            # Write the data and say we're ready for more.
            f.write(buf)
            done += bytes_read
            print("ACK")
"""


class REPLHub:
    """Run scripts on generic MicroPython boards with a REPL over USB."""

    EOL = b"\r\n"  # MicroPython EOL

    def __init__(self):
        self.reset_buffers()

    def reset_buffers(self):
        """Resets internal buffers that track (parsed) serial data."""
        self.print_output = False
        self.output = []
        self.buffer = b""
        self.log_file = None
        try:
            self.serial.read(self.serial.in_waiting)
        except AttributeError:
            pass

    async def connect(self, device=None):
        """Connects to a SPIKE Prime or MINDSTORMS Inventor Hub."""

        # Go through all comports.
        port = None
        devices = list_ports.comports()
        for dev in devices:
            if dev.product == "LEGO Technic Large Hub in FS Mode" or dev.vid == 0x0694:
                port = dev.device
                break

        # Raise error if there is no hub.
        if port is None:
            raise OSError("Could not find hub.")

        # Open the serial connection.
        print("Connecting to {0}".format(port))
        self.serial = Serial(port)
        self.serial.read(self.serial.in_waiting)
        print("Connected!")

    async def disconnect(self):
        """Disconnects from the hub."""
        self.serial.close()

    def parse_input(self):
        """Reads waiting serial data and parse as needed."""
        data = self.serial.read(self.serial.in_waiting)
        self.buffer += data

    def is_idle(self, key=b">>> "):
        """Checks if REPL is ready for a new command."""
        self.parse_input()
        return self.buffer[-len(key) :] == key

    async def reset_hub(self):
        """Soft resets the hub to clear MicroPython variables."""

        # Cancel anything that is running
        for i in range(5):
            self.serial.write(b"\x03")
            await asyncio.sleep(0.1)

        # Soft reboot
        self.serial.write(b"\x04")
        await asyncio.sleep(0.5)

        # Prevent runtime from coming up
        while not self.is_idle():
            self.serial.write(b"\x03")
            await asyncio.sleep(0.1)

        # Clear all buffers
        self.reset_buffers()

        # Load file transfer function
        await self.exec_paste_mode(FILE_TRANSFER_SCRIPT, print_output=False)
        self.reset_buffers()

        print("Hub is ready.")

    async def exec_line(self, line, wait=True):
        """Executes one line on the REPL."""

        # Initialize
        self.reset_buffers()
        encoded = line.encode()
        start_len = len(self.buffer)

        # Write the command and prepare expected echo.
        echo = encoded + b"\r\n"
        self.serial.write(echo)

        # Wait until the echo has been read.
        while len(self.buffer) < start_len + len(echo):
            await asyncio.sleep(0.05)
            self.parse_input()
        # Raise error if we did not get the echo back.
        if echo not in self.buffer[start_len:]:
            print(start_len, self.buffer, self.buffer[start_len - 1 :], echo)
            raise ValueError("Failed to execute line: {0}.".format(line))

        # We are done if we don't want to wait for the result.
        if not wait:
            return

        # Wait for MicroPython to execute the command.
        while not self.is_idle():
            await asyncio.sleep(0.1)

    line_handler = PybricksHub.line_handler

    async def exec_paste_mode(self, code, wait=True, print_output=True):
        """Executes commands via paste mode."""

        # Initialize buffers
        self.reset_buffers()
        self.print_output = print_output

        # Convert script string to binary.
        encoded = code.encode()

        # Enter paste mode.
        self.serial.write(b"\x05")
        while not self.is_idle(key=b"=== "):
            await asyncio.sleep(0.1)

        # Paste the script, chunk by chunk to avoid overrun
        start_len = len(self.buffer)
        echo = encoded + b"\r\n"

        for c in chunk(echo, 200):
            self.serial.write(c)
            # Wait until the pasted code is echoed back.
            while len(self.buffer) < start_len + len(c):
                await asyncio.sleep(0.05)
                self.parse_input()

            # If it isn't, then stop.
            if c not in self.buffer[start_len:]:
                print(start_len, self.buffer, self.buffer[start_len - 1 :], echo)
                raise ValueError("Failed to paste: {0}.".format(code))

            start_len += len(c)

        # Parse hub output until the script is done.
        line_index = len(self.buffer)
        self.output = []

        # Exit paste mode and start executing.
        self.serial.write(b"\x04")

        # If we don't want to wait, we are done.
        if not wait:
            return

        # Look for output while the program runs
        while not self.is_idle():

            # Keep parsing hub data.
            self.parse_input()

            # Look for completed lines that we haven't parsed yet.
            next_line_index = self.buffer.find(self.EOL, line_index)

            if next_line_index >= 0:
                # If a new line is found, parse it.
                self.line_handler(self.buffer[line_index:next_line_index])
                line_index = next_line_index + len(self.EOL)
            await asyncio.sleep(0.1)

        # Parse remaining hub data.
        while (next_line_index := self.buffer.find(self.EOL, line_index)) >= 0:
            self.line_handler(self.buffer[line_index:next_line_index])
            line_index = next_line_index + len(self.EOL)

    async def run(self, py_path, wait=True, print_output=True):
        """Executes a script via paste mode."""
        script = open(py_path).read()
        self.script_dir, _ = os.path.split(py_path)
        await self.reset_hub()
        await self.exec_paste_mode(script, wait, print_output)

    async def upload_file(self, destination, contents):
        """Uploads a file to the hub."""

        # Print upload info.
        size = len(contents)
        print(f"Uploading {destination} ({size} bytes)")
        self.reset_buffers()

        # Prepare hub to receive file
        await self.exec_line(f"receive_file('{destination}', {size})", wait=False)

        ACK = b"ACK" + self.EOL
        progress = 0

        # Write file chunk by chunk.
        for data in chunk(contents, FILE_PACKET_SIZE):

            # Send a chunk and wait for acknowledgement of receipt
            buffer_now = len(self.buffer)
            progress += self.serial.write(data)
            while len(self.buffer) < buffer_now + len(ACK):
                await asyncio.sleep(0.01)
                self.parse_input()

            # Raise error if we didn't get acknowledgement
            if self.buffer[buffer_now : buffer_now + len(ACK)] != ACK:
                print(self.buffer[buffer_now:])
                raise ValueError("Did not get expected response from the hub.")

            # Print progress
            print(f"Progress: {int(progress / size * 100)}%", end="\r")

        # Get REPL back in normal state
        await self.exec_line("# File transfer complete")
